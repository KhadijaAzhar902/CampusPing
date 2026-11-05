"""CampusPing Phone: checks UMT LMS and sends new items + deadlines to Telegram.

  python main.py --once     one check, then exit
  python main.py            check every 15 minutes (leave running)
  python main.py --dry      print messages instead of sending
"""
import json, os, re, sys, time, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from icalendar import Calendar
import lms
import dashboard
import deep

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PKT = ZoneInfo("Asia/Karachi")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
INTERVAL_MIN = 15
DRY = "--dry" in sys.argv
ICONS = {"assign": "📝", "quiz": "❓", "resource": "📄", "folder": "📁", "forum": "📣",
         "url": "🔗", "page": "📃", "book": "📖", "lesson": "🎓", "post": "📣", "pluginfile": "📄", "update": "✏️",
         "label": "🆕"}


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
            print("ntfy failed:", type(e).__name__)
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
            print("email failed:", type(e).__name__)
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        try:
            requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                          json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30).raise_for_status()
            sent = True
        except Exception as e:
            print("telegram failed:", type(e).__name__)
    if not sent:
        # GitHub's build logs are public: never print message text (course names, files) there.
        print("[no channel delivered the message: check EMAIL_USER / EMAIL_APP_PASS]")


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


# Courses you dropped / don't want alerts for (matched against the start of the LMS course name).
# Add more codes here, or set EXCLUDE_COURSES="AI395,CS4135" in .env / GitHub.
EXCLUDE = tuple(c.strip().upper() for c in
                (os.getenv("EXCLUDE_COURSES") or "AI395,CS4135").split(",") if c.strip())


def excluded(fullname):
    return fullname.strip().upper().startswith(EXCLUDE)


def clean(text):
    text = re.sub(r"&\w*;?\.{3}$", "", text or "")  # the LMS truncates names mid-entity: '...&nbs...'
    return re.sub(r"\s+", " ", html.unescape(text)).replace("✦", "").strip()


def collect(s, deep_labels=True, prev_labels=None):
    """deep_labels: also open every week page to find handouts hidden in text blocks (slower)."""
    sesskey = lms._sesskey(s.get(f"{lms.BASE}/my/", timeout=30).text)
    courses = []
    for cls in ("inprogress", "future"):  # 'future' = enrolled but not started yet
        courses += ajax(s, sesskey, "core_course_get_enrolled_courses_by_timeline_classification",
                        {"offset": 0, "limit": 0, "classification": cls, "sort": "fullname",
                         "customfieldname": "", "customfieldvalue": ""})["courses"]
    items, errors, cm_week = {}, [], {}
    for c in courses:
        if excluded(c["fullname"]):
            continue
        cname = short_course(c["fullname"])
        try:
            st = json.loads(ajax(s, sesskey, "core_courseformat_get_state", {"courseid": c["id"]}))
        except Exception as e:
            errors.append(f"{cname}: {e}")
            continue
        label_sections, forum_cms = set(), []
        titles = {x.get("number"): clean(x.get("title", "")) for x in st.get("section", [])}
        week_of = lambda n: titles.get(n, "") if re.match(r"week\b", titles.get(n, ""), re.I) else ""
        for cm in st.get("cm", []):
            if not cm.get("visible", True):
                continue
            week = week_of(cm.get("sectionnumber"))
            if cm.get("module") == "label":
                label_sections.add(cm.get("sectionnumber"))
                if clean(cm["name"]):  # a text block: only worth an alert if the teacher adds a new one
                    items[f"cm{cm['id']}"] = {"course": cname, "name": clean(cm["name"])[:140], "week": week,
                                              "module": "label", "url": f"{lms.BASE}/course/view.php?id={c['id']}"}
                continue
            cm_week[(int(c["id"]), int(cm["id"]))] = week  # the LMS gives some ids as text
            if cm.get("module") == "forum":
                forum_cms.append(cm["id"])
            items[f"cm{cm['id']}"] = {"course": cname, "name": clean(cm["name"]), "week": week,
                                      "module": cm.get("module", ""), "url": cm.get("url", "")}
        # --- things the course list hides: forum posts (every run) and handouts inside text blocks ---
        try:
            posts, e1 = deep.forum_posts(s, lms.BASE, forum_cms)
            for k, p in posts.items():
                items[k] = {"course": cname, **p}
            errors += [f"{cname}: {e}" for e in e1]
            if deep_labels:
                links, e2 = deep.label_links(s, lms.BASE, c["id"], sorted(x for x in label_sections if x is not None),
                                             titles)
                for k, p in links.items():
                    items[k] = {"course": cname, "week": week_of(p.get("section")), **p}
                errors += [f"{cname}: {e}" for e in e2]
        except Exception as e:
            errors.append(f"{cname} deep scan: {e}")
    deep_failed = any("section" in e for e in errors)
    if prev_labels and (not deep_labels or deep_failed):  # reuse last scan's handout list so nothing flaps
        items.update({k: v for k, v in prev_labels.items() if k not in items})
    # deadlines from the Timeline (not limited to ~60 days like the calendar export)
    deadlines, after, now = {}, None, int(time.time())
    while True:
        args = {"timesortfrom": now - 7 * 86400, "timesortto": now + 800 * 86400, "limitnum": 50}
        if after:
            args["aftereventid"] = after
        evs = ajax(s, sesskey, "core_calendar_get_action_events_by_timesort", args)["events"]
        for e in evs:
            if excluded(e["course"]["fullnamedisplay"]):
                continue
            due = datetime.fromtimestamp(e["timesort"], timezone.utc)
            m = re.search(r"[?&]id=(\d+)", e.get("url") or "")
            deadlines[f"ev{e['id']}"] = {"title": e["name"].replace(" is due", ""),
                                         "course": short_course(e["course"]["fullnamedisplay"]),
                                         "week": cm_week.get((int(e["course"]["id"]), int(m.group(1))), "") if m else "",
                                         "due": due.isoformat()}
        if len(evs) < 50:
            break
        after = evs[-1]["id"]
    return items, deadlines, errors


def direct_url(s, it, cache):
    """Web-link items (Drive, YouTube, ...) open on an LMS 'click here' page. Return the real address."""
    if it["module"] != "url" or "/mod/url/" not in it["url"]:
        return it["url"]
    if it["url"] in cache:
        return cache[it["url"]]
    try:
        page = s.get(it["url"], timeout=30).text
        m = re.search(r'urlworkaround[^>]*>.*?<a href="([^"]+)"', page, re.S)
        final = html.unescape(m.group(1)) if m else it["url"]
        if not final.startswith(("http://", "https://")):
            final = it["url"]
    except Exception:
        final = it["url"]
    cache[it["url"]] = final
    return final


def wk(x):
    """A line telling which week of the course an item belongs to (empty if unknown)."""
    return f"\n📍 {x['week']}" if x.get("week") else ""


def fmt_due(iso):
    d = datetime.fromisoformat(iso).astimezone(PKT)
    return d.strftime("%a %d %b, %I:%M %p")


def check():
    s = lms.login()
    st = load_state()
    key = os.getenv("DASH_KEY")
    prev = (dashboard.previous(key) or {}) if key else {}
    cache = dict(prev.get("resolved", {}))
    now = datetime.now(timezone.utc)
    # Hourly deep scan of week pages (handouts hidden in text blocks); forum posts are checked every run.
    deep_due = (st is None or not st.get("deep_baselined") or not prev.get("labels")
                or time.time() - st.get("last_deep", 0) > 55 * 60)
    items, deadlines, errors = collect(s, deep_labels=deep_due, prev_labels=prev.get("labels"))
    if not items:  # logged in but saw nothing: the login/page layout probably changed
        raise RuntimeError("LMS returned no courses or items (login or layout may have changed)")
    last_deep = time.time() if deep_due else (st or {}).get("last_deep", 0)
    resolve = lambda it: direct_url(s, it, cache)
    if st is None:  # first run: remember what exists, don't spam
        upcoming = sorted((d for d in deadlines.values() if datetime.fromisoformat(d["due"]) > now),
                          key=lambda d: d["due"])
        lines = ["✅ <b>CampusPing is connected.</b>",
                 f"Watching {len({i['course'] for i in items.values()})} courses, {len(items)} items."]
        if upcoming:
            lines.append("\n<b>Upcoming deadlines</b>")
            lines += [f"• {html.escape(d['title'])} — {fmt_due(d['due'])}" + (f" ({d['week']})" if d.get("week") else "")
                      for d in upcoming[:10]]
        send("\n".join(lines))
        save_state({"items": list(items), "deadlines": {k: v["due"] for k, v in deadlines.items()},
                    "reminded": {}, "deep_baselined": True, "last_deep": last_deep})
        update_dashboard(items, deadlines, [], resolve, cache)
        return
    seen, known_dl, reminded = set(st["items"]), st["deadlines"], st["reminded"]
    events = []
    if not st.get("deep_baselined"):
        # One-time catch-up: handouts hidden in text blocks and forum posts were never scanned before,
        # so send them as ONE summary instead of dozens of separate alerts.
        old = {k: it for k, it in items.items() if k not in seen and k[:2] in ("lb", "fd")}
        if old:
            by_course = {}
            for it in old.values():
                by_course.setdefault(it["course"], []).append(it)
            lines = [f"🔎 <b>Catch-up: {len(old)} earlier items CampusPing had missed</b>",
                     "These were already on your LMS (hidden handouts and forum posts)."]
            for course, lst in sorted(by_course.items()):
                lines.append(f"\n<b>{html.escape(course)}</b>")
                lines += [f"• {html.escape(i['name'])}" + (f" ({i['week']})" if i.get("week") else "") + f"\n  {i['url']}"
                          for i in lst[:25]]
                if len(lst) > 25:
                    lines.append(f"  …and {len(lst) - 25} more")
            send("\n".join(lines))
        seen |= set(old)
        # the 23 template text blocks per course already exist: remember them silently
        seen |= {k for k, it in items.items() if it["module"] == "label" or k.startswith("tx")}

    def log(kind, title, course, url="", week=""):
        events.append({"t": now.isoformat(), "kind": kind, "title": title, "course": course, "url": url, "week": week})
    for k, it in items.items():
        if k not in seen:
            icon = ICONS.get(it["module"], "🆕")
            link = resolve(it)
            what = {"post": "New post", "pluginfile": "New handout", "label": "New note", "update": "Week text updated"}.get(it["module"], "New")
            send(f"{icon} <b>{what} in {html.escape(it['course'])}</b>\n{html.escape(it['name'])}{wk(it)}\n{link}")
            log("new", it["name"], it["course"], link, it.get("week", ""))
    for k, d in deadlines.items():
        due = datetime.fromisoformat(d["due"])
        if k not in known_dl:
            if due > now:
                send(f"⏰ <b>New deadline</b>\n{html.escape(d['title'])}\n{html.escape(d['course'])}{wk(d)}\nDue {fmt_due(d['due'])}")
                log("deadline", d["title"], d["course"], week=d.get("week", ""))
        elif known_dl[k] != d["due"]:
            send(f"🔄 <b>Deadline changed</b>\n{html.escape(d['title'])}{wk(d)}\nNow due {fmt_due(d['due'])}")
            log("changed", d["title"], d["course"], week=d.get("week", ""))
            reminded.pop(k + "|24", None); reminded.pop(k + "|3", None)
        for hrs in (24, 3):
            tag = f"{k}|{hrs}"
            if tag not in reminded and now < due <= now + timedelta(hours=hrs):
                send(f"⚠️ <b>Due in under {hrs} hours</b>\n{html.escape(d['title'])}\n{html.escape(d['course'])}{wk(d)}\n{fmt_due(d['due'])}")
                reminded[tag] = True
                log("reminder", f"{d['title']} (under {hrs}h)", d["course"], week=d.get("week", ""))
    if errors:
        # course names appear in these details; hide them in public CI logs, show them on your own PC
        print("warnings:", f"{len(errors)} page(s) could not be read" if os.getenv("GITHUB_ACTIONS") else errors)
    save_state({"items": list(seen | set(items)),
                "deadlines": {**known_dl, **{k: v["due"] for k, v in deadlines.items()}},
                "reminded": reminded, "deep_baselined": True, "last_deep": last_deep})
    update_dashboard(items, deadlines, events, resolve, cache)


def update_dashboard(items, deadlines, events, resolve, cache):
    key = os.getenv("DASH_KEY")
    if not key:
        return
    try:
        if dashboard.write(key, items, deadlines, events, resolve, cache):
            print("dashboard data updated")
    except Exception as e:
        print("dashboard update failed:", type(e).__name__)


FAIL_AFTER = 3          # consecutive failed runs (about 45 min) before you get an email
REMIND_AFTER_H = 24     # while it stays broken, remind once a day


def run_check():
    """Run one check; if the LMS can't be read, count failures and email you (once, then daily)."""
    before = load_state() or {}
    try:
        check()  # a successful check saves fresh state, which resets the failure counter
    except Exception as e:
        reason = re.sub(r"(sesskey|token|password)=[^&\s'\"]+", r"\1=***", f"{type(e).__name__}: {str(e)[:200]}")
        print(f"::error::CampusPing check failed: {reason}")
        st = load_state()
        if st is None:
            return
        st["fails"] = st.get("fails", 0) + 1
        due = (not st.get("fail_alerted")) or time.time() - st.get("fail_alert_t", 0) > REMIND_AFTER_H * 3600
        if st["fails"] >= FAIL_AFTER and due:
            send("🚨 <b>CampusPing can't read your LMS</b>\n"
                 f"It has failed {st['fails']} checks in a row, so you may be missing updates.\n"
                 f"Reason: {html.escape(reason)}\n"
                 "Check the LMS yourself for now. Common causes: changed LMS password, LMS down or "
                 "blocking, or a layout change.")
            st["fail_alerted"], st["fail_alert_t"] = True, time.time()
        save_state(st)
        return
    if before.get("fail_alerted"):
        send("✅ <b>CampusPing is working again</b>\nIt can read your LMS again. Anything posted while it "
             "was down will show up as normal new alerts.")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_check()
    else:
        while True:
            run_check()
            time.sleep(INTERVAL_MIN * 60)
