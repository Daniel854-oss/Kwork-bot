import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ── Telegram ──────────────────────────────────────────────
TG_BOT_TOKEN_ORDERS = os.getenv("TG_BOT_TOKEN_ORDERS") or os.getenv("TG_BOT_TOKEN", "")
TG_BOT_TOKEN_MESSAGES = os.getenv("TG_BOT_TOKEN_MESSAGES", "")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0"))

# ── AI ────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── Kwork defaults ────────────────────────────────────────
MIN_BUDGET = int(os.getenv("MIN_BUDGET", "0"))
KWORK_OFFER_TYPE = os.getenv("KWORK_OFFER_TYPE", "custom")

# ── Accounts ──────────────────────────────────────────────
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts.json")


def load_accounts_config() -> list[dict]:
    """Load account configs from accounts.json with login/password from env."""
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    accounts = []
    for acc in raw:
        login = os.getenv(acc["login_env"], "")
        password = os.getenv(acc["password_env"], "")
        if not login or not password:
            log.warning("Account %s: missing credentials (%s/%s)", acc["name"], acc["login_env"], acc["password_env"])
            continue
        accounts.append({
            "id": acc["id"],
            "name": acc["name"],
            "login": login,
            "password": password,
            "price": acc.get("price", 1000),
            "duration": acc.get("duration", 3),
            "services": [s.lower() for s in acc.get("services", [])],
        })

    if not accounts:
        log.error("No accounts configured! Check env vars and accounts.json")
    return accounts
