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


# ── Training data ─────────────────────────────────────────

TRAINING_FILE = os.path.join(os.path.dirname(__file__), "training_data.json")

def load_training_data() -> dict:
    """Returns {"offers": [...], "replies": [...]}."""
    if not os.path.exists(TRAINING_FILE):
        return {"offers": [], "replies": []}
    with open(TRAINING_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_training_data(data: dict):
    with open(TRAINING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_training_offer(order_desc: str, offer_text: str, price: int = 0, days: int = 0):
    """Save a good offer as training example."""
    data = load_training_data()
    data["offers"].append({
        "order": order_desc[:300],
        "offer": offer_text,
        "price": price,
        "days": days,
    })
    # Keep last 50
    data["offers"] = data["offers"][-50:]
    save_training_data(data)


def add_training_reply(client_msg: str, my_reply: str):
    """Save a good reply as training example."""
    data = load_training_data()
    data["replies"].append({
        "client": client_msg[:300],
        "reply": my_reply,
    })
    data["replies"] = data["replies"][-50:]
    save_training_data(data)

