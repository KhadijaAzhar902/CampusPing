"""Read-only Moodle client for lms.umt.edu.pk: login, calendar feed, course list."""
import os, re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("LMS_BASE", "https://lms.umt.edu.pk").rstrip("/")
UA = {"User-Agent": "CampusPing/1.0 (personal student notifier)"}


class LoginFailed(Exception):
    pass


def login():
    s = requests.Session()
    s.headers.update(UA)
    page = s.get(f"{BASE}/login/index.php", timeout=30)
    soup = BeautifulSoup(page.text, "html.parser")
    tok = soup.find("input", {"name": "logintoken"})
    data = {"username": os.environ["LMS_USER"], "password": os.environ["LMS_PASS"]}
    if tok:
        data["logintoken"] = tok["value"]
    r = s.post(f"{BASE}/login/index.php", data=data, timeout=30)
    if "login/index.php" in r.url or "loginerrormessage" in r.text:
        raise LoginFailed("LMS login failed (wrong password or extra verification step).")
    return s


def _sesskey(html):
    m = re.search(r'"sesskey":"([^"]+)"', html) or re.search(r"sesskey=([A-Za-z0-9]+)", html)
    return m.group(1) if m else None


def calendar_feed_url(s):
    """Ask Moodle for this user's private export URL (the 'Get calendar URL' button)."""
    page = s.get(f"{BASE}/calendar/export.php", timeout=30)
    sesskey = _sesskey(page.text)
    r = s.post(
        f"{BASE}/calendar/export.php",
        data={
            "sesskey": sesskey,
            "_qf__core_calendar_export_form": 1,
            "events[exportevents]": "all",
            "period[timeperiod]": "recentupcoming",
            "generateurl": "Get calendar URL",
        },
        timeout=30,
    )
    m = re.search(r"https?://[^\s\"'<>]*export_execute\.php\?[^\s\"'<>]+", r.text)
    if not m:
        raise RuntimeError("Could not find calendar export URL on the LMS page.")
    return m.group(0).replace("&amp;", "&")


def fetch_ics(s, url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text
