from __future__ import annotations

import logging
from dataclasses import dataclass, field
from kwork import Kwork

from config import load_accounts_config

log = logging.getLogger(__name__)


@dataclass
class Account:
    id: str
    name: str
    login: str
    password: str
    price: int = 1000
    duration: int = 3
    services: list[str] = field(default_factory=list)

    def create_api(self) -> Kwork:
        return Kwork(login=self.login, password=self.password)


class AccountManager:
    def __init__(self):
        self.accounts: list[Account] = []
        self._load()

    def _load(self):
        configs = load_accounts_config()
        self.accounts = [Account(**cfg) for cfg in configs]
        log.info("Loaded %d accounts: %s", len(self.accounts), [a.name for a in self.accounts])

    def get(self, account_id: str) -> Account | None:
        for acc in self.accounts:
            if acc.id == account_id:
                return acc
        return None

    def match_account(self, title: str, desc: str) -> Account:
        """Auto-route: pick account with most keyword matches in title+desc."""
        text = (title + " " + desc).lower()
        best: Account | None = None
        best_score = 0
        for acc in self.accounts:
            score = sum(1 for s in acc.services if s in text)
            if score > best_score:
                best_score = score
                best = acc
        return best or self.accounts[0]

    def list_names(self) -> list[str]:
        return [f"{a.name} ({a.id})" for a in self.accounts]
