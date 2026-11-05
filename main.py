"""CampusPing Phone: checks UMT LMS and sends new items + deadlines to Telegram.

  python main.py --once     one check, then exit
  python main.py            check every 15 minutes (leave running)
  python main.py --dry      print messages instead of sending
"""
import json, os, sys, time, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from icalendar import Calendar
import lms

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PKT = ZoneInfo("Asia/Karachi")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
INTERVAL_MIN = 15
DRY = "--dry" in sys.argv
ICONS = {"assign": "📝", "quiz": "❓", "resource": "📄", "folder": "📁", "forum": "📣",
         "url": "🔗", "page": "📃", "book": "📖", "lesson": "🎓"}


def plain(text):
    return html.unescape(text.replace("<b>", "").replace("</b>", ""))


def send(text):
    """Send via every channel configured in .env (email, ntfy, telegram)."""
    p = plain(text)
    sent = False
    if DRY:
        print("[message]\n" + p + "\n")
        return
    title, _, body = p.partition("\n")
    if os.getenv("NTFY_TOPIC"):
        try:
            requests.post(f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", data=(body or title).encode("utf-8"),
                          headers={"Title": title.encode("utf-8"), "Priority": "high"}, timeout=30).raise_for_status()
            sent = True
        except Exception as e:
            print("ntfy failed:", e)
    if os.getenv("EMAIL_USER") and os.getenv("EMAIL_APP_PASSWORD"):
        try:
            import smtplib
            from email.message import EmailMessage
            m = EmailMessage()
            m["Subject"], m["From"] = title, os.environ["EMAIL_USER"]
            m["To"] = os.getenv("EMAIL_TO") or os.environ["EMAIL_USER"]
            m.set_content(body or title)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(os.environ["EMAIL_USER"], os.environ["EMAIL_APP_PASSWORD"])
                smtp.send_message(m)
            sent = True
        except Exception as e:
            print("email failed:", e)
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        try:
            requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                          json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30).raise_for_status()
            sent = True
        except Exception as e:
            print("telegram failed:", e)
    if not sent:
        print("[no channel configured]\n" + p + "\n")


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return None


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), indent=1)


def ajax(s, sesskey, method, args):
    r = s.post(f"{lms.BASE}/lib/ajax/service.php?sesskey={sesskey}&info={method}",
               json=[{"index": 0, "methodname": method, "args": args}], timeout=60).json()[0]
    if r.get("error"):
        raise RuntimeError(f"{method}: {r.get('exception', {}).get('message', r)}")
    return r["data"]


def short_course(name):
    # 'AI395 - Generative AI - Dr. X - V1 - OC' -> 'AI395 Generative AI'
    parts = [p.strip() for p in name.split(" - ")]
    return " ".join(parts[:2]) if len(parts) > 1 else name


def collect(s):
    sesskey = lms._sesskey(s.get(f"{lms.BASE}/my/", timeout=30).text)
    courses = []
    for cls in ("inprogress", "future"):  # 'future' = enrolled but not started yet
        courses += ajax(s, sesskey, "core_course_get_enrolled_courses_by_timeline_classification",
                        {"offset": 0, "limit": 0, "classification": cls, "sort": "fullname",
                         "customfieldname": "", "customfieldvalue": ""})["courses"]
    items, errors = {}, []
    for c in courses:
        cname = short_course(c["fullname"])
        try:
            st = json.loads(ajax(s, sesskey, "core_courseformat_get_state", {"courseid": c["id"]}))
        except Exception as e:
            errors.append(f"{cname}: {e}")
            continue
        for cm in st.get("cm", []):
            if cm.get("visible", True) and cm.get("module") != "label":
                items[f"cm{cm['id']}"] = {"course": cname, "name": cm["name"],
                                          "module": cm.get("module", ""), "url": cm.get("url", "")}
    # deadlines from the Timeline (not limited to ~60 days like the calendar export)
    deadlines, after, now = {}, None, int(time.time())
    while True:
        args = {"timesortfrom": now - 7 * 86400, "timesortto": now + 800 * 86400, "limitnum": 50}
        if after:
            args["aftereventid"] = after
        evs = ajax(s, sesskey, "core_calendar_get_action_events_by_timesort", args)["events"]
        for e in evs:
            due = datetime.fromtimestamp(e["timesort"], timezone.utc)
            deadlines[f"ev{e['id']}"] = {"title": e["name"].replace(" is due", ""),
                                         "course": short_course(e["course"]["fullnamedisplay"]),
                                         "due": due.isoformat()}
        if len(evs) < 50:
            break
        after = evs[-1]["id"]
    return items, deadlines, errors


def fmt_due(iso):
    d = datetime.fromisoformat(iso).astimezone(PKT)
    return d.strftime("%a %d %b, %I:%M %p")


def check():
    s = lms.login()
    items, deadlines, errors = collect(s)
    st = load_state()
    now = datetime.now(timezone.utc)
    if st is None:  # first run: remember what exists, don't spam
        upcoming = sorted((d for d in deadlines.values() if datetime.fromisoformat(d["due"]) > now),
                          key=lambda d: d["due"])
        lines = ["✅ <b>CampusPing is connected.</b>",
                 f"Watching {len({i['course'] for i in items.values()})} courses, {len(items)} items."]
        if upcoming:
            lines.append("\n<b>Upcoming deadlines</b>")
            lines += [f"• {html.escape(d['title'])} — {fmt_due(d['due'])}" for d in upcoming[:10]]
        send("\n".join(lines))
        save_state({"items": list(items), "deadlines": {k: v["due"] for k, v in deadlines.items()},
                    "reminded": {}})
        return
    seen, known_dl, reminded = set(st["items"]), st["deadlines"], st["reminded"]
    for k, it in items.items():
        if k not in seen:
            icon = ICONS.get(it["module"], "🆕")
            send(f"{icon} <b>New in {html.escape(it['course'])}</b>\n{html.escape(it['name'])}\n{it['url']}")
    for k, d in deadlines.items():
        due = datetime.fromisoformat(d["due"])
        if k not in known_dl:
            if due > now:
                send(f"⏰ <b>New deadline</b>\n{html.escape(d['title'])}\n{html.escape(d['course'])}\nDue {fmt_due(d['due'])}")
        elif known_dl[k] != d["due"]:
            send(f"🔄 <b>Deadline changed</b>\n{html.escape(d['title'])}\nNow due {fmt_due(d['due'])}")
            reminded.pop(k + "|24", None); reminded.pop(k + "|3", None)
        for hrs in (24, 3):
            tag = f"{k}|{hrs}"
            if tag not in reminded and now < due <= now + timedelta(hours=hrs):
                send(f"⚠️ <b>Due in under {hrs} hours</b>\n{html.escape(d['title'])}\n{html.escape(d['course'])}\n{fmt_due(d['due'])}")
                reminded[tag] = True
    if errors:
        print("warnings:", errors)
    save_state({"items": list(set(st["items"]) | set(items)),
                "deadlines": {**known_dl, **{k: v["due"] for k, v in deadlines.items()}},
                "reminded": reminded})


if __name__ == "__main__":
    if "--once" in sys.argv:
        check()
    else:
        while True:
            try:
                check()
            except Exception as e:
                print(datetime.now(), "check failed:", e)
            time.sleep(INTERVAL_MIN * 60)
