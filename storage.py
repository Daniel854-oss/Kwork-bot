import json
import os

KEYWORDS_FILE = os.path.join(os.path.dirname(__file__), "keywords.json")
BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "blacklist.json")
SEEN_IDS_FILE = os.path.join(os.path.dirname(__file__), "seen_ids.json")
SEEN_MSGS_FILE = os.path.join(os.path.dirname(__file__), "seen_msgs.json")


# ── Keywords ──────────────────────────────────────────────

def load_keywords() -> list[str]:
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_keywords(kws: list[str]):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(kws, f, ensure_ascii=False, indent=2)


# ── Blacklist ─────────────────────────────────────────────

def load_blacklist() -> list[str]:
    if not os.path.exists(BLACKLIST_FILE):
        return []
    with open(BLACKLIST_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_blacklist(bl: list[str]):
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(bl, f, ensure_ascii=False, indent=2)


def is_blacklisted(title: str, desc: str) -> bool:
    bl = [w.lower() for w in load_blacklist()]
    text = (title + " " + desc).lower()
    return any(w in text for w in bl)


# ── Seen order IDs ────────────────────────────────────────

def load_seen() -> set[int]:
    if not os.path.exists(SEEN_IDS_FILE):
        return set()
    with open(SEEN_IDS_FILE, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(ids: set[int]):
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


# ── Seen message IDs ─────────────────────────────────────

def load_seen_msgs() -> dict[str, list[int]]:
    """Returns {account_id: [message_ids]}."""
    if not os.path.exists(SEEN_MSGS_FILE):
        return {}
    with open(SEEN_MSGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_seen_msgs(data: dict[str, list[int]]):
    with open(SEEN_MSGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
