import time
import logging
from typing import Dict, Any
from utils import safe_json_load, safe_json_write_atomic

log = logging.getLogger("memecoinsnipa.storage")

PLAYBOOK_FILE = "playbook.json"

DEFAULT_STATE = {
    "scans": 0,
    "picks": [],
    "trades": [],
    "blacklist": [],
    "cooldowns": {},
    "stats": {
        "wins": 0,
        "losses": 0,
        "total_return": 0.0
    }
}


class Storage:
    def __init__(self):
        self.state: Dict[str, Any] = safe_json_load(PLAYBOOK_FILE, DEFAULT_STATE)

    def save(self):
        safe_json_write_atomic(PLAYBOOK_FILE, self.state)

    def increment_scan(self):
        self.state["scans"] += 1

    def add_pick(self, pick: Dict[str, Any]):
        pick["timestamp"] = int(time.time())
        self.state["picks"].append(pick)

    def add_trade(self, trade: Dict[str, Any]):
        trade["timestamp"] = int(time.time())
        self.state["trades"].append(trade)

    def update_stats(self, win: bool, return_pct: float):
        if win:
            self.state["stats"]["wins"] += 1
        else:
            self.state["stats"]["losses"] += 1

        self.state["stats"]["total_return"] += return_pct

    def add_blacklist(self, token: str):
        if token not in self.state["blacklist"]:
            self.state["blacklist"].append(token)

    def is_blacklisted(self, token: str) -> bool:
        return token in self.state.get("blacklist", [])

    def set_cooldown(self, key: str, seconds: int):
        self.state["cooldowns"][key] = int(time.time()) + seconds

    def cooldown_active(self, key: str) -> bool:
        until = self.state.get("cooldowns", {}).get(key)
        if not until:
            return False
        return time.time() < until

    def prune_old_data(self, max_trades: int = 1000):
        if len(self.state["trades"]) > max_trades:
            self.state["trades"] = self.state["trades"][-max_trades:]
