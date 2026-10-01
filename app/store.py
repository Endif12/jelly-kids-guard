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
    # max_eps: 最多集数；soft_minutes: B 软上限；hard_total: C 硬上限；
    # max_sessions: 观看次数(0=不限)；gap_hours: 两次之间需间隔小时(0=不要求)。
    # 0 均为关闭该项。
    return {"max_eps": 2, "soft_minutes": 40, "hard_total": 60,
            "max_sessions": 0, "gap_hours": 0}


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
        "fallback_episode_minutes": 25,
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
            old = ((rule or {}).get("days") or {}).get(str(i), {})
            # Migrate v1 key max_minutes -> soft_minutes (B)
            day = {"max_eps": 2, "soft_minutes": 40, "hard_total": 60,
                   "max_sessions": 0, "gap_hours": 0}
            if "max_minutes" in old and "soft_minutes" not in old:
                old = dict(old)
                old["soft_minutes"] = old.pop("max_minutes")
            day.update(old)
            days[str(i)] = {
                "max_eps": int(day.get("max_eps", 2) or 0),
                "soft_minutes": int(day.get("soft_minutes", 40) or 0),
                "hard_total": int(day.get("hard_total", 60) or 0),
                "max_sessions": int(day.get("max_sessions", 0) or 0),
                "gap_hours": float(day.get("gap_hours", 0) or 0),
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

    def today_rule(self, user_id: str) -> dict:
        return self.get_user(user_id)["days"].get(str(self.weekday()), default_day_rule())

    def effective_limits(self, user_id: str):
        """Return (max_eps, soft_B, hard_C, min_A) for today incl. bonus.

        Bonus only applies on the day it was granted (bonus_day guard).
        Non-positive max/soft/hard means unlimited/off; A <= 0 disables guarantee.
        Bonus extends B and C (extra time), never the episode count cap.
        """
        rule = self.get_user(user_id)
        day = rule["days"].get(str(self.weekday()), default_day_rule())
        max_eps = int(day.get("max_eps", 0) or 0)
        soft_b = int(day.get("soft_minutes", 0) or 0)
        hard_c = int(day.get("hard_total", 0) or 0)
        if rule.get("bonus_day") == self.today_key():
            soft_b += int(rule.get("bonus_minutes", 0) or 0)
            hard_c += int(rule.get("bonus_minutes", 0) or 0)
            max_eps += int(rule.get("bonus_eps", 0) or 0)
        return max_eps, soft_b, hard_c, int(rule.get("min_minutes", 0) or 0)
