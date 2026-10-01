"""Persistent settings + per-user rule storage (no hand-written config files)."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

COUNT_SCOPES = {
    "episode": "仅电视剧集 (Episode)",
    "episode_movie": "剧集 + 电影 (Episode, Movie)",
    "all": "全部播放记录",
}

SCOPE_ITEM_TYPES = {
    "episode": ["Episode"],
    "episode_movie": ["Episode", "Movie"],
    "all": [],
}


def default_day_rule():
    return {"max_eps": 2, "max_minutes": 60}


def default_user_rule():
    return {
        "enabled": True,
        "min_minutes": 0,
        "keep_folders": [],
        "bonus_eps": 0,
        "bonus_minutes": 0,
        "bonus_day": "",
        "days": {str(i): default_day_rule() for i in range(7)},
    }


def default_settings():
    return {
        "server": {"host": "", "token": ""},
        "polling_minutes": 1.0,
        "count_scope": "episode",
        "users": {},
    }


def _migrate(settings: dict) -> dict:
    base = default_settings()
    for key, val in base.items():
        if key not in settings:
            settings[key] = copy.deepcopy(val)
    for uid, rule in list(settings.get("users", {}).items()):
        full = default_user_rule()
        full.update(rule or {})
        days = {}
        for i in range(7):
            day = {"max_eps": 2, "max_minutes": 60}
            day.update(((rule or {}).get("days") or {}).get(str(i), {}))
            days[str(i)] = {
                "max_eps": int(day.get("max_eps", 2) or 0),
                "max_minutes": int(day.get("max_minutes", 60) or 0),
            }
        full["days"] = days
        full["min_minutes"] = int(full.get("min_minutes", 0) or 0)
        settings["users"][uid] = full
    return settings


class SettingsStore:
    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self.data = default_settings()
        self.load()

    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = _migrate(json.load(fh))
            except (ValueError, OSError):
                self.data = default_settings()
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def server(self):
        return self.data.setdefault("server", {"host": "", "token": ""})

    def is_configured(self) -> bool:
        return bool(self.server.get("host") and self.server.get("token"))

    def get_user(self, user_id: str) -> dict:
        users = self.data.setdefault("users", {})
        if user_id not in users:
            users[user_id] = default_user_rule()
        return users[user_id]

    def today_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def weekday(self) -> int:
        return datetime.now().weekday()  # 0 = Monday

    def effective_limits(self, user_id: str):
        """Return (max_eps, max_minutes, min_minutes) for today incl. bonus.

        Bonus only applies on the day it was granted (bonus_day guard).
        Non-positive max_* means unlimited.
        """
        rule = self.get_user(user_id)
        day = rule["days"].get(str(self.weekday()), default_day_rule())
        max_eps = int(day.get("max_eps", 0) or 0)
        max_minutes = int(day.get("max_minutes", 0) or 0)
        if rule.get("bonus_day") == self.today_key():
            max_eps += int(rule.get("bonus_eps", 0) or 0)
            max_minutes += int(rule.get("bonus_minutes", 0) or 0)
        return max_eps, max_minutes, int(rule.get("min_minutes", 0) or 0)
