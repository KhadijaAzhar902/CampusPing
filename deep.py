"""Deep scan: finds content that the normal course listing hides.

 1. Links/handouts embedded in 'label' text blocks (inside each week).
 2. Posts (announcements) inside forums.
Both need to read pages, so this is run for every course; see main.py for how often.
"""
import hashlib, re, html, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup

SKIP_HOSTS = ("download.moodle.org", "moodle.com", "moodle.org")


def _name_from_link(a, href):
    text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
    if "/pluginfile.php/" in href:  # uploaded file: the real file name is the most useful label
        return urllib.parse.unquote(urllib.parse.urlparse(href).path.rsplit("/", 1)[-1])[:140]
    if text and text.lower() not in ("here", "click here", "link", "download"):
        return text[:140]
    tail = urllib.parse.unquote(urllib.parse.urlparse(href).path.rsplit("/", 1)[-1])
    return (tail or text or href)[:140]


def label_links(session, base, course_id, section_numbers):
    """{key: item} for every useful link inside label blocks of the given sections."""
    found = {}

    def one(sec):
        for attempt in (1, 2):
            try:
                r = session.get(f"{base}/course/view.php?id={course_id}&section={sec}", timeout=45)
                r.raise_for_status()
                return sec, r.text
            except Exception as e:
                err = e
        return sec, err

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, section_numbers))
    errors = []
    for sec, page in results:
        if isinstance(page, Exception):
            errors.append(f"section {sec}: {page}")
            continue
        soup = BeautifulSoup(page, "html.parser")
        for li in soup.select("li.activity.modtype_label, li[id^=module-].modtype_label"):
            cmid = (li.get("id") or "").replace("module-", "")
            for junk in li.select("button, .dropdown, .activity-completion, [data-region=completion-info], "
                                  ".completion-dialog, .visually-hidden, .sr-only, .accesshide"):
                junk.decompose()  # per-student UI ('Mark as done', menus): ticking it must not look like an edit
            text = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
            text = re.sub(r"(Select activity|Mark as done|Done)", "", text).strip()
            if len(text) > 15:  # fingerprint of the block's wording: changes when the teacher edits it
                found[f"tx{cmid}|{hashlib.md5(text.encode()).hexdigest()[:10]}"] = {
                    "name": f"Week {sec}: {text[:90]}", "module": "update", "section": sec,
                    "url": f"{base}/course/view.php?id={course_id}&section={sec}"}
            for a in li.find_all("a", href=True):
                href = html.unescape(a["href"]).strip()
                if not href.startswith(("http://", "https://")):
                    continue
                host = urllib.parse.urlparse(href).netloc
                if host.endswith(SKIP_HOSTS):
                    continue
                if host == urllib.parse.urlparse(base).netloc and not ("/pluginfile.php/" in href or "/mod/" in href):
                    continue  # links back into the LMS (course page, profile...) aren't content
                clean = href.split("#")[0]
                key = f"lb{cmid}|{clean}"
                found[key] = {"name": _name_from_link(a, clean), "url": clean,
                              "module": "pluginfile" if "/pluginfile.php/" in clean else "url",
                              "section": sec}
    return found, errors


def forum_posts(session, base, forum_cmids):
    """{key: item} for discussion threads in each forum (cm id -> list of posts)."""
    def one(cmid):
        try:
            r = session.get(f"{base}/mod/forum/view.php?id={cmid}", timeout=45)
            r.raise_for_status()
            return cmid, r.text
        except Exception as e:
            return cmid, e

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, forum_cmids))
    found, errors = {}, []
    for cmid, page in results:
        if isinstance(page, Exception):
            errors.append(f"forum {cmid}: {page}")
            continue
        soup = BeautifulSoup(page, "html.parser")
        for a in soup.select('a[href*="/mod/forum/discuss.php?d="]'):
            m = re.search(r"discuss\.php\?d=(\d+)", a["href"])
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip(' "“”')
            if not m or not title or title.lower() in ("discuss this topic", "permalink"):
                continue
            key = f"fd{m.group(1)}"
            found.setdefault(key, {"name": title[:140], "url": f"{base}/mod/forum/discuss.php?d={m.group(1)}",
                                   "module": "post", "forum": cmid})
    return found, errors
