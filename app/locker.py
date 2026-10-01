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
from rules import (IDLE_MINUTES, MIN_COUNTED_MINUTES, boundary_lock, clusters,
                   next_gate, session_gate, split_sessions)
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
                rule["bonus_eps_base"] = None
                rule["bonus_min"] = 0
                rule["bonus_min_base"] = None
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

    def _ensure_bonus_day(self, rule: dict):
        if rule.get("bonus_day") != self.settings.today_key():
            rule["bonus_day"] = self.settings.today_key()
            rule["bonus_eps"] = 0
            rule["bonus_eps_base"] = None
            rule["bonus_min"] = 0
            rule["bonus_min_base"] = None

    def bonus_remaining(self, user_id: str, day_eps: int, day_mins: int) -> tuple[int, int]:
        """规则外剩余加时 (剩几集, 剩几分钟)，按发放时刻的今日累计扣减."""
        rule = self.settings.get_user(user_id)
        if rule.get("bonus_day") != self.settings.today_key():
            return 0, 0
        eps_left, min_left = 0, 0
        if int(rule.get("bonus_eps", 0) or 0) > 0:
            base = rule.get("bonus_eps_base")
            consumed = max(0, day_eps - base) if base is not None else 0
            eps_left = max(0, int(rule["bonus_eps"]) - consumed)
        if int(rule.get("bonus_min", 0) or 0) > 0:
            base = rule.get("bonus_min_base")
            consumed = max(0, day_mins - base) if base is not None else 0
            min_left = max(0, int(rule["bonus_min"]) - consumed)
        return eps_left, min_left

    def grant_episode(self, user_id: str):
        """规则外多看 1 集（跳过集数/时长/次数/冷却全部规则）。"""
        rule = self.settings.get_user(user_id)
        self._ensure_bonus_day(rule)
        if int(rule.get("bonus_eps", 0) or 0) <= 0:
            eps, _ = self.stats.today(user_id, *self._day_range())
            rule["bonus_eps_base"] = eps
        rule["bonus_eps"] = int(rule.get("bonus_eps", 0) or 0) + 1
        self.settings.save()

    def grant_minutes(self, user_id: str, minutes: int = 5):
        """规则外多看 N 分钟（跳过全部规则，当前集播完前不掐）。"""
        rule = self.settings.get_user(user_id)
        self._ensure_bonus_day(rule)
        if int(rule.get("bonus_min", 0) or 0) <= 0:
            _, mins = self.stats.today(user_id, *self._day_range())
            rule["bonus_min_base"] = mins
        rule["bonus_min"] = int(rule.get("bonus_min", 0) or 0) + minutes
        self.settings.save()

    def clear_bonus(self, user_id: str):
        """撤销加时（按错了用这个），规则立即恢复。"""
        rule = self.settings.get_user(user_id)
        rule["bonus_eps"] = 0
        rule["bonus_eps_base"] = None
        rule["bonus_min"] = 0
        rule["bonus_min_base"] = None
        rule["bonus_day"] = ""
        self.settings.save()

    def reset_today(self, user_id: str) -> tuple[bool, str]:
        """清零今日播放记录（测试用，不可恢复），同时撤销加时."""
        start, end = self._day_range()
        ok, msg = self.stats.reset_day(user_id, start, end)
        self.clear_bonus(user_id)
        return ok, msg

    def check_user(self, user_id: str, playing: dict | None = None) -> dict:
        """Poll one user, enforce, return status dict for the UI.

        A/B/C settle per viewing session (counters reset each time);
        the day only carries quota + cooldown. playing optionally
        carries one shared /Sessions snapshot for all users.
        """
        from datetime import datetime, timedelta

        start, end = self._day_range()
        now = datetime.now()
        day_eps, day_mins = self.stats.today(user_id, start, end)
        bonus_eps, bonus_min = self.bonus_remaining(user_id, day_eps, day_mins)
        bonus_active = bonus_eps > 0 or bonus_min > 0
        rows = self.stats.today_rows(user_id, start, end) if not bonus_active else []
        groups = clusters(rows)
        if playing is None:
            try:
                playing = self.client.now_playing()
            except Exception:  # noqa: BLE001
                playing = {}
        active = user_id in (playing or {})
        fresh = bool(groups) and (now - groups[-1]["end"]) <= timedelta(minutes=IDLE_MINUTES)
        # active + stale/empty = 刚起播新的一次（插件还没记上），计数从 0 开始；
        # 不 active = 两次之间，A/B/C 不结算（由次数+间隔管下一次能不能起播）。
        ongoing = active and fresh
        if ongoing:
            cur = groups[-1]["rows"]
            done_groups = groups[:-1]
        else:
            cur = []
            done_groups = groups
        # 误触几分钟的不算用掉一次
        done_groups = [g for g in done_groups
                       if sum(s for _, s in g["rows"]) >= MIN_COUNTED_MINUTES * 60]
        completed = len(done_groups)
        last_end = done_groups[-1]["end"] if done_groups else None
        sess_eps = len(cur)
        sess_mins = sum(s for _, s in cur) // 60
        max_eps, soft_b, hard_c, min_a = self.settings.effective_limits(user_id)
        day = self.settings.today_rule(user_id)
        max_sess = int(day.get("max_sessions", 0) or 0)
        gap_h = float(day.get("gap_hours", 0) or 0)
        if bonus_active:
            parts = []
            if bonus_eps > 0:
                parts.append(f"剩 {bonus_eps} 集")
            if bonus_min > 0:
                parts.append(f"剩约 {bonus_min} 分钟")
            locked, reason = False, "规则外加时中（" + " / ".join(parts) + "），本次播完前不锁"
            est_next, est_src = 0, ""
            resume_at = None
        else:
            locked, reason = boundary_lock(sess_eps, sess_mins, max_eps, soft_b, min_a)
            est_next, est_src = 0, ""
            if not locked and hard_c > 0 and sess_mins > 0:
                est_next, est_src = self.estimate_next_minutes(user_id, day_eps, day_mins)
                locked, reason = next_gate(sess_mins, est_next, hard_c)
            resume_at = None
            if not locked and max_sess > 0:
                locked, reason, resume_at = session_gate(completed, last_end, now, max_sess, gap_h)
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
            "sess_eps": sess_eps, "sess_mins": sess_mins,
            "day_eps": day_eps, "day_mins": day_mins,
            "ongoing": ongoing,
            "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
            "min_a": min_a, "est_next": est_next, "est_src": est_src,
            "sess_used": completed, "sess_max": max_sess,
            "bonus_eps": bonus_eps, "bonus_min": bonus_min,
            "resume_at": resume_at.strftime("%H:%M") if resume_at else "",
            "locked": locked, "reason": reason,
            "folders": folders,
        }
        self.status[user_id] = st
        return st

    def check_all(self):
        self._maybe_new_day()
        if not self.settings.is_configured():
            return {}
        try:
            playing = self.client.now_playing()
        except Exception:  # noqa: BLE001
            playing = {}
        for uid, rule in self.settings.data.get("users", {}).items():
            if rule.get("enabled"):
                try:
                    self.check_user(uid, playing)
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
