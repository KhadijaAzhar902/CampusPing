"""Builds the encrypted dashboard data (docs/data.enc.json).

The repo is public, so the real course names/deadlines are AES-256-GCM encrypted with a
passphrase (secret DASH_KEY). The page in docs/index.html decrypts it in the browser.
"""
import base64, json, os
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.enc.json")
ITERATIONS = 200_000
MAX_HISTORY = 60


def _key(passphrase, salt):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(passphrase.encode("utf-8"))


def _decrypt(passphrase, blob):
    salt, iv, ct = (base64.b64decode(blob[k]) for k in ("s", "i", "c"))
    return json.loads(AESGCM(_key(passphrase, salt)).decrypt(iv, ct, None))


def _encrypt(passphrase, payload):
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(iv, json.dumps(payload).encode("utf-8"), None)
    return {"v": 1, "iter": ITERATIONS, **{k: base64.b64encode(v).decode() for k, v in
                                          (("s", salt), ("i", iv), ("c", ct))}}


def previous(passphrase):
    try:
        return _decrypt(passphrase, json.load(open(OUT, encoding="utf-8")))
    except Exception:
        return None


def write(passphrase, items, deadlines, new_events):
    """items/deadlines come from main.collect(); new_events are alerts sent in this run."""
    old = previous(passphrase) or {}
    history = (new_events + old.get("history", []))[:MAX_HISTORY]
    courses = {}
    for k, it in items.items():
        courses.setdefault(it["course"], []).append((int(k[2:]), it))
    payload = {
        "courses": [{"name": name, "count": len(lst),
                     "latest": [{"name": i["name"], "module": i["module"], "url": i["url"]}
                                for _, i in sorted(lst, key=lambda x: -x[0])[:5]]}
                    for name, lst in sorted(courses.items())],
        "deadlines": sorted(({"title": d["title"], "course": d["course"], "due": d["due"]}
                             for d in deadlines.values()), key=lambda d: d["due"]),
        "history": history,
    }
    # Only rewrite when something changed, so we don't make a commit every 15 minutes.
    if old and all(old.get(k) == payload[k] for k in payload):
        return False
    payload["generated"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(_encrypt(passphrase, payload), open(OUT, "w", encoding="utf-8"))
    return True
