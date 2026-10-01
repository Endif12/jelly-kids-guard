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
            logger.info("new day %s: reset bonus/sitting + restore folders", today)
            self.last_day = today
            for uid, rule in self.settings.data.get("users", {}).items():
                rule["bonus_eps"] = 0
                rule["bonus_eps_base"] = None
                rule["bonus_min"] = 0
                rule["bonus_min_base"] = None
                rule["bonus_day"] = ""
                sess = rule.setdefault("sess", {})
                sess.update({"date": today, "used": 0, "since": None, "cooldown_until": None,
                             "open": True, "last_eps": None, "last_mins": None})
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

    def _sess(self, user_id: str) -> dict:
        """取今日 sitting 状态（含跨天滚动），调用方改完后需 save。"""
        rule = self.settings.get_user(user_id)
        st = rule.setdefault("sess", {"date": "", "used": 0, "since": None,
                                      "cooldown_until": None, "open": True,
                                      "last_eps": None, "last_mins": None})
        today = self.settings.today_key()
        if st.get("date") != today:
            st.update({"date": today, "used": 0, "since": None, "cooldown_until": None,
                       "open": True, "last_eps": None, "last_mins": None})
        return st

    @staticmethod
    def _parse(ts):
        from datetime import datetime

        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    def _reset_sitting(self, user_id: str):
        """开启新的一轮（冷却到点 / 手动解锁 / 加时发放时调用，不碰 quota）。"""
        from datetime import datetime

        st = self._sess(user_id)
        st["since"] = datetime.now().isoformat(timespec="seconds")
        st["cooldown_until"] = None
        st["open"] = True

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
        if self.status.get(user_id, {}).get("locked"):
            self._reset_sitting(user_id)  # 上一轮已结束，加时开启新的一轮
        self.settings.save()

    def grant_minutes(self, user_id: str, minutes: int = 5):
        """规则外多看 N 分钟（跳过全部规则，当前集播完前不掐）。"""
        rule = self.settings.get_user(user_id)
        self._ensure_bonus_day(rule)
        if int(rule.get("bonus_min", 0) or 0) <= 0:
            _, mins = self.stats.today(user_id, *self._day_range())
            rule["bonus_min_base"] = mins
        rule["bonus_min"] = int(rule.get("bonus_min", 0) or 0) + minutes
        if self.status.get(user_id, {}).get("locked"):
            self._reset_sitting(user_id)  # 上一轮已结束，加时开启新的一轮
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
        """清零今日播放记录（测试用，不可恢复），同时撤销加时、重置轮次."""
        start, end = self._day_range()
        ok, msg = self.stats.reset_day(user_id, start, end)
        self.clear_bonus(user_id)
        sess = self._sess(user_id)
        sess.update({"used": 0, "since": None, "cooldown_until": None,
                     "open": True, "last_eps": None, "last_mins": None,
                     "date": self.settings.today_key()})
        self.settings.save()
        return ok, msg

    def _apply_folders(self, user_id: str, folders: list, locked: bool, reason: str = ""):
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
        max_eps, soft_b, hard_c, min_a = self.settings.effective_limits(user_id)
        day = self.settings.today_rule(user_id)
        max_sess = int(day.get("max_sessions", 0) or 0)
        gap_h = float(day.get("gap_hours", 0) or 0)
        sess = self._sess(user_id)
        dirty = False

        bonus_eps, bonus_min = self.bonus_remaining(user_id, day_eps, day_mins)
        if bonus_eps > 0 or bonus_min > 0:
            since = self._parse(sess.get("since"))
            rows = self.stats.today_rows(user_id, start, end)
            cur = [r for r in rows if since is None or r[0] >= since]
            st = {
                "sess_eps": len(cur), "sess_mins": sum(s for _, s in cur) // 60,
                "day_eps": day_eps, "day_mins": day_mins,
                "ongoing": bool(playing) and user_id in (playing or {}),
                "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
                "min_a": min_a, "est_next": 0, "est_src": "",
                "sess_used": int(sess.get("used", 0) or 0), "sess_max": max_sess,
                "bonus_eps": bonus_eps, "bonus_min": bonus_min,
                "resume_at": "",
                "locked": False,
                "reason": ("规则外加时中（" + " / ".join(
                    ([f"剩 {bonus_eps} 集"] if bonus_eps else []) +
                    ([f"剩约 {bonus_min} 分钟"] if bonus_min else [])) + "），本次播完前不锁"),
                "folders": self.client.get_enabled_folders(user_id),
            }
            self._apply_folders(user_id, st["folders"], locked=False)
            self.status[user_id] = st
            return st

        # 冷却到点 -> 自动解锁并开启新的一轮
        cd = self._parse(sess.get("cooldown_until"))
        if cd is not None and now >= cd:
            self._reset_sitting(user_id)
            dirty = True

        open_round = bool(sess.get("open", True))
        since = self._parse(sess.get("since"))
        rows = self.stats.today_rows(user_id, start, end)
        if open_round:
            cur = [r for r in rows if since is None or r[0] >= since]
        else:
            cur = []  # 轮已结束：锁后还播的尾巴不计入任何一轮
        sess_eps = len(cur)
        sess_mins = sum(s for _, s in cur) // 60
        locked, reason = False, ""
        abc_fired = False
        est_next, est_src = 0, ""
        if open_round:
            locked, reason = boundary_lock(sess_eps, sess_mins, max_eps, soft_b, min_a)
            abc_fired = locked
            if not locked and hard_c > 0 and sess_mins > 0:
                est_next, est_src = self.estimate_next_minutes(user_id, day_eps, day_mins)
                locked, reason = next_gate(sess_mins, est_next, hard_c)
                abc_fired = abc_fired or locked
        resume_at = None
        used = int(sess.get("used", 0) or 0)
        if not locked and max_sess > 0:
            if used >= max_sess:
                locked, reason = True, f"今日已完成 {used} 次（上限 {max_sess} 次），明天再看"
            else:
                cd2 = self._parse(sess.get("cooldown_until"))
                if cd2 is not None and now < cd2:
                    left = cd2 - now
                    hrs, rem = divmod(int(left.total_seconds()), 3600)
                    desc = f"{hrs} 小时 {rem // 60} 分" if hrs else f"{rem // 60} 分"
                    locked = True
                    reason = f"冷却中，还差约 {desc}（{cd2.strftime('%H:%M')}后可看下一次）"
                    resume_at = cd2
        if locked and abc_fired and max_sess > 0:
            # ABC 触发 = 这一轮结束：冻结数字、记次数、关轮
            used += 1
            sess["used"] = used
            sess["since"] = now.isoformat(timespec="seconds")
            sess["open"] = False
            sess["last_eps"] = sess_eps
            sess["last_mins"] = sess_mins
            if gap_h > 0 and used < max_sess:
                sess["cooldown_until"] = (now + timedelta(hours=gap_h)).isoformat(timespec="seconds")
                resume_at = now + timedelta(hours=gap_h)
                reason = reason + f"，第 {used} 次结束，冷却至 {resume_at.strftime('%H:%M')}"
            else:
                sess["cooldown_until"] = None
                if used < max_sess:
                    sess["open"] = True  # 间隔=0：新一轮紧接着开
            dirty = True
        if dirty:
            self.settings.save()
        folders = self.client.get_enabled_folders(user_id)
        self._apply_folders(user_id, folders, locked, reason if locked else "")

        # 展示：开轮显示实时累计；关轮（名额用完/冷却中）显示冻结数字
        if sess.get("open", True) or sess.get("last_eps") is None:
            disp_eps, disp_mins = sess_eps, sess_mins
        else:
            disp_eps, disp_mins = sess["last_eps"], sess["last_mins"]
        st = {
            "sess_eps": disp_eps, "sess_mins": disp_mins,
            "day_eps": day_eps, "day_mins": day_mins,
            "ongoing": bool(playing) and user_id in (playing or {}),
            "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
            "min_a": min_a, "est_next": est_next, "est_src": est_src,
            "sess_used": used, "sess_max": max_sess,
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
        self._reset_sitting(user_id)  # 再给一次完整机会，quota 不变
        self.settings.save()
        backup = self._recall(user_id)
        if backup:
            self.client.set_enabled_folders(user_id, backup)
