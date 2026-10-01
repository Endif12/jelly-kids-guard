"""Enforcement loop: poll stats, soft-lock (revoke libraries) on over-limit.

Soft lock never cuts the running stream (Jellyfin keeps playing what
already started), which implements "等播完当前集再锁" automatically:
the next episode / movie can no longer be opened.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import yaml

from jellyfin_client import JellyfinClient
from rules import boundary_lock, next_gate
from stats import PlaybackStats
from store import SettingsStore

logger = logging.getLogger("kids-guard")


class Guard:
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.client: JellyfinClient | None = None
        self.stats: PlaybackStats | None = None
        self.backup_path = os.path.join(os.environ.get("DATA_DIR", "data"), "folders.bck")
        self.last_day = settings.today_key()
        self.status: dict[str, dict] = {}
        self.reconnect()

    # ----- wiring -----------------------------------------------------
    def reconnect(self):
        from store import SCOPE_ITEM_TYPES

        srv = self.settings.server
        self.client = JellyfinClient(srv.get("host", ""), srv.get("token", ""))
        scope = self.settings.data.get("count_scope", "episode")
        self.stats = PlaybackStats(self.client, SCOPE_ITEM_TYPES.get(scope, ["Episode"]))

    # ----- folder backup ----------------------------------------------
    def _read_backup(self) -> dict:
        if os.path.isfile(self.backup_path):
            try:
                with open(self.backup_path, "r", encoding="utf-8") as fh:
                    return yaml.safe_load(fh) or {}
            except (ValueError, OSError):
                pass
        return {}

    def _write_backup(self, data: dict):
        os.makedirs(os.path.dirname(self.backup_path) or ".", exist_ok=True)
        with open(self.backup_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh)

    def _remember(self, user_id: str, folders: list):
        if not folders:
            return
        data = self._read_backup()
        data[user_id] = list(folders)
        self._write_backup(data)

    def _recall(self, user_id: str) -> list:
        return list(self._read_backup().get(user_id, []) or [])

    # ----- main loop ---------------------------------------------------
    def _day_range(self):
        now = datetime.now()
        return now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")

    def _maybe_new_day(self):
        today = self.settings.today_key()
        if today != self.last_day:
            logger.info("new day %s: reset bonus + restore folders", today)
            self.last_day = today
            for uid, rule in self.settings.data.get("users", {}).items():
                rule["bonus_eps"] = 0
                rule["bonus_minutes"] = 0
                rule["bonus_day"] = ""
                if rule.get("enabled"):
                    backup = self._recall(uid)
                    if backup:
                        self.client.set_enabled_folders(uid, backup)
            self.settings.save()

    def estimate_next_minutes(self, user_id: str, eps: int, mins: int) -> tuple[int, str]:
        """Cascade: real NextUp > series average > today's average > fallback."""
        try:
            est, src = self.client.next_episode_minutes(user_id)
        except Exception:  # noqa: BLE001
            logger.exception("next-episode lookup failed for %s", user_id)
            est, src = 0, ""
        if est > 0:
            return est, src
        if eps > 0 and mins > 0:
            return mins // eps, "today_avg"
        fallback = int(self.settings.data.get("fallback_episode_minutes", 25) or 0)
        return (fallback, "fallback") if fallback > 0 else (0, "")

    def check_user(self, user_id: str) -> dict:
        """Poll one user, enforce, return status dict for the UI."""
        start, end = self._day_range()
        eps, mins = self.stats.today(user_id, start, end)
        max_eps, soft_b, hard_c, min_a = self.settings.effective_limits(user_id)
        locked, reason = boundary_lock(eps, mins, max_eps, soft_b, min_a)
        est_next, est_src = 0, ""
        if not locked:
            est_next, est_src = self.estimate_next_minutes(user_id, eps, mins)
            locked, reason = next_gate(mins, est_next, hard_c)
        folders = self.client.get_enabled_folders(user_id)
        rule = self.settings.get_user(user_id)
        keep = [f for f in folders if f in (rule.get("keep_folders") or [])]

        if locked:
            only_unlimited = not folders or all(f in (rule.get("keep_folders") or []) for f in folders)
            if not only_unlimited:
                self._remember(user_id, folders)
            if folders != keep:
                self.client.set_enabled_folders(user_id, keep)
                logger.info("lock %s: %s", user_id, reason)
        else:
            if folders:
                self._remember(user_id, folders)
            backup = self._recall(user_id)
            if backup and len(backup) > len(folders):
                self.client.set_enabled_folders(user_id, backup)
                logger.info("restore %s", user_id)

        st = {
            "eps": eps, "mins": mins,
            "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
            "min_a": min_a, "est_next": est_next, "est_src": est_src,
            "locked": locked, "reason": reason,
            "folders": folders,
        }
        self.status[user_id] = st
        return st

    def check_all(self):
        self._maybe_new_day()
        if not self.settings.is_configured():
            return {}
        for uid, rule in self.settings.data.get("users", {}).items():
            if rule.get("enabled"):
                try:
                    self.check_user(uid)
                except Exception:  # keep polling other users
                    logger.exception("check failed for %s", uid)
        return self.status

    def manual_lock(self, user_id: str):
        rule = self.settings.get_user(user_id)
        folders = self.client.get_enabled_folders(user_id)
        if folders:
            self._remember(user_id, folders)
        keep = [f for f in folders if f in (rule.get("keep_folders") or [])]
        self.client.set_enabled_folders(user_id, keep)

    def manual_unlock(self, user_id: str):
        backup = self._recall(user_id)
        if backup:
            self.client.set_enabled_folders(user_id, backup)
