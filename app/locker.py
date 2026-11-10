"""Enforcement loop: poll stats, soft-lock (revoke libraries) on over-limit.

Soft lock never cuts the running stream (Jellyfin keeps playing what
already started), which implements "等播完当前集再锁" automatically:
the next episode / movie can no longer be opened.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta

import yaml

from jellyfin_client import JellyfinClient
from rules import boundary_lock, next_gate
from stats import PlaybackStats
from store import SettingsStore

logger = logging.getLogger("kids-guard")

ITEM_TTL = 600  # 秒：条目元数据（剧名/总时长）缓存时长


class Guard:
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.client: JellyfinClient | None = None
        self.stats: PlaybackStats | None = None
        self.backup_path = os.path.join(os.environ.get("DATA_DIR", "data"), "folders.bck")
        self.last_day = settings.today_key()
        self.status: dict[str, dict] = {}
        # 后台线程与网页按钮并发访问 Guard，用可重入锁保证
        # 轮次/次数/加时等状态不被双计数。
        self._lock = threading.RLock()
        self._item_cache: dict[str, tuple[float, dict]] = {}
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
        with self._lock:
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
                    rule["manual_locked"] = False
                    sess = rule.setdefault("sess", {})
                    sess.update({"date": today, "used": 0, "since": None,
                                 "cooldown_until": None, "open": True,
                                 "last_eps": None, "last_mins": None,
                                 "last_secs": None, "last_since": None})
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
                                      "last_eps": None, "last_mins": None,
                                      "last_secs": None, "last_since": None})
        today = self.settings.today_key()
        if st.get("date") != today:
            st.update({"date": today, "used": 0, "since": None, "cooldown_until": None,
                       "open": True, "last_eps": None, "last_mins": None,
                       "last_secs": None, "last_since": None})
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
        with self._lock:
            rule = self.settings.get_user(user_id)
            rule["manual_locked"] = False  # 显式放行盖过手动锁
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
        with self._lock:
            rule = self.settings.get_user(user_id)
            rule["manual_locked"] = False  # 显式放行盖过手动锁
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
        with self._lock:
            rule = self.settings.get_user(user_id)
            rule["bonus_eps"] = 0
            rule["bonus_eps_base"] = None
            rule["bonus_min"] = 0
            rule["bonus_min_base"] = None
            rule["bonus_day"] = ""
            self.settings.save()

    def reset_today(self, user_id: str) -> tuple[bool, str]:
        """清零今日播放记录（测试用，不可恢复），同时撤销加时/手动锁、重置轮次."""
        with self._lock:
            start, end = self._day_range()
            ok, msg = self.stats.reset_day(user_id, start, end)
            self.clear_bonus(user_id)
            rule = self.settings.get_user(user_id)
            rule["manual_locked"] = False
            sess = self._sess(user_id)
            sess.update({"used": 0, "since": None, "cooldown_until": None,
                         "open": True, "last_eps": None, "last_mins": None,
                         "last_secs": None, "last_since": None,
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
        with self._lock:
            return self._check_user_locked(user_id, playing)

    def _item_meta(self, user_id: str, item_ids: list[str]) -> dict[str, dict]:
        """Item metadata with TTL cache; batch lookup, never raises."""
        now_ts = time.time()
        need = [iid for iid in dict.fromkeys(item_ids) if iid
                and (iid not in self._item_cache
                     or now_ts - self._item_cache[iid][0] > ITEM_TTL)]
        if need:
            try:
                infos = self.client.items_info(user_id, need)
            except Exception:  # noqa: BLE001
                logger.exception("items_info failed")
                infos = {}
            for iid, info in infos.items():
                self._item_cache[iid] = (now_ts, info)
        out = {}
        for iid in item_ids:
            cached = self._item_cache.get(iid)
            if cached:
                out[iid] = cached[1]
        return out

    def _episode_window(self, sess: dict, max_sess: int,
                        used: int, records: list[dict]) -> list[dict]:
        """挑出与看板“本次”口径一致的那批记录（按转换后的最终状态选窗）."""
        open_now = bool(sess.get("open", True))
        since = self._parse(sess.get("since"))
        if open_now:
            return [r for r in records if since is None or r["ts"] >= since]
        if max_sess > 0 and used >= max_sess:
            return list(records)  # 配额用完：本次=今日
        # 冷却中：显示刚结束那一轮 [last_since, since)
        last_since = self._parse(sess.get("last_since"))
        sel = [r for r in records
               if (last_since is None or r["ts"] >= last_since)
               and (since is not None and r["ts"] < since)]
        return sel or list(records)

    def _build_episodes(self, user_id: str, records: list[dict]) -> list[dict]:
        """把记录转成看板明细行：剧名（海报页标题）+ 播放时长/总时长."""
        if not records:
            return []
        meta = self._item_meta(user_id, [r["item_id"] for r in records])
        episodes = []
        for r in records:
            m = meta.get(r["item_id"], {})
            series = m.get("series") or r.get("name") or "未知节目"
            episodes.append({"series": series, "played": r["secs"],
                             "total": int(m.get("total_secs") or 0)})
        return episodes

    def _check_user_locked(self, user_id: str, playing: dict | None = None) -> dict:
        from datetime import datetime, timedelta

        start, end = self._day_range()
        now = datetime.now()
        # 单快照：今日和本次都从同一批明细行算，避免两次查询之间
        # 正在播的记录又涨了几分钟导致两边对不上。
        records = self.stats.today_records(user_id, start, end)
        rows_all = [(r["ts"], r["secs"]) for r in records]
        day_eps = len(rows_all)
        day_mins = sum(s for _, s in rows_all) // 60
        max_eps, soft_b, hard_c, min_a = self.settings.effective_limits(user_id)
        day = self.settings.today_rule(user_id)
        max_sess = int(day.get("max_sessions", 0) or 0)
        gap_h = float(day.get("gap_hours", 0) or 0)
        sess = self._sess(user_id)
        dirty = False

        if self.settings.get_user(user_id).get("manual_locked"):
            since = self._parse(sess.get("since"))
            cur = [r for r in rows_all if since is None or r[0] >= since]
            st = {
                "sess_eps": len(cur), "sess_mins": sum(s for _, s in cur) // 60,
                "sess_secs": sum(s for _, s in cur),
                "day_eps": day_eps, "day_mins": day_mins,
                "day_secs": sum(s for _, s in rows_all),
                "ongoing": bool(playing) and user_id in (playing or {}),
                "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
                "min_a": min_a, "est_next": 0, "est_src": "",
                "sess_used": int(sess.get("used", 0) or 0), "sess_max": max_sess,
                "bonus_eps": 0, "bonus_min": 0,
                "resume_at": "",
                "checked_at": now.strftime("%H:%M:%S"),
                "playing_title": (playing or {}).get(user_id, ""),
                "locked": True,
                "reason": "手动锁定（立即锁），点撤销立即锁恢复",
                "folders": self.client.get_enabled_folders(user_id),
            }
            since_d = self._parse(sess.get("since"))
            cur_m = [r for r in records if since_d is None or r["ts"] >= since_d]
            st["episodes"] = self._build_episodes(user_id, cur_m)
            self._apply_folders(user_id, st["folders"], locked=True, reason=st["reason"])
            self.status[user_id] = st
            return st

        bonus_eps, bonus_min = self.bonus_remaining(user_id, day_eps, day_mins)
        if bonus_eps > 0 or bonus_min > 0:
            since = self._parse(sess.get("since"))
            cur = [r for r in rows_all if since is None or r[0] >= since]
            cur_recs = [r for r in records if since is None or r["ts"] >= since]
            st = {
                "sess_eps": len(cur), "sess_mins": sum(s for _, s in cur) // 60,
                "sess_secs": sum(s for _, s in cur),
                "day_eps": day_eps, "day_mins": day_mins,
                "day_secs": sum(s for _, s in rows_all),
                "ongoing": bool(playing) and user_id in (playing or {}),
                "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
                "min_a": min_a, "est_next": 0, "est_src": "",
                "sess_used": int(sess.get("used", 0) or 0), "sess_max": max_sess,
                "bonus_eps": bonus_eps, "bonus_min": bonus_min,
                "resume_at": "",
                "checked_at": now.strftime("%H:%M:%S"),
                "playing_title": (playing or {}).get(user_id, ""),
                "locked": False,
                "reason": ("规则外加时中（" + " / ".join(
                    ([f"剩 {bonus_eps} 集"] if bonus_eps else []) +
                    ([f"剩约 {bonus_min} 分钟"] if bonus_min else [])) + "），本次播完前不锁"),
                "folders": self.client.get_enabled_folders(user_id),
            }
            st["episodes"] = self._build_episodes(user_id, cur_recs)
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
        if open_round:
            cur = [r for r in rows_all if since is None or r[0] >= since]
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
            sess["last_since"] = sess.get("since")  # 刚结束这轮的起点，明细展示用
            sess["since"] = now.isoformat(timespec="seconds")
            sess["open"] = False
            sess["last_eps"] = sess_eps
            sess["last_mins"] = sess_mins
            sess["last_secs"] = sum(s for _, s in cur)
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

        # 展示：开轮显示实时累计；关轮显示冻结数字；配额用完则本轮即全天。
        if sess.get("open", True) or sess.get("last_eps") is None:
            disp_eps, disp_mins = sess_eps, sess_mins
            disp_secs = sum(s for _, s in cur)
        elif max_sess > 0 and used >= max_sess:
            disp_eps, disp_mins, disp_secs = day_eps, day_mins, sum(s for _, s in rows_all)
        else:
            disp_eps, disp_mins = sess["last_eps"], sess["last_mins"]
            disp_secs = sess.get("last_secs") or disp_mins * 60
        st = {
            "sess_eps": disp_eps, "sess_mins": disp_mins, "sess_secs": disp_secs,
            "day_eps": day_eps, "day_mins": day_mins,
            "day_secs": sum(s for _, s in rows_all),
            "ongoing": bool(playing) and user_id in (playing or {}),
            "max_eps": max_eps, "soft_b": soft_b, "hard_c": hard_c,
            "min_a": min_a, "est_next": est_next, "est_src": est_src,
            "sess_used": used, "sess_max": max_sess,
            "bonus_eps": 0, "bonus_min": 0,
            "resume_at": resume_at.strftime("%H:%M") if resume_at else "",
            "checked_at": now.strftime("%H:%M:%S"),
            "playing_title": (playing or {}).get(user_id, ""),
            "locked": locked, "reason": reason,
            "folders": folders,
        }
        st["episodes"] = self._build_episodes(
            user_id, self._episode_window(sess, max_sess, used, records))
        self.status[user_id] = st
        return st

    def check_all(self):
        with self._lock:
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
        with self._lock:
            rule = self.settings.get_user(user_id)
            folders = self.client.get_enabled_folders(user_id)
            if folders:
                self._remember(user_id, folders)
            keep = [f for f in folders if f in (rule.get("keep_folders") or [])]
            self.client.set_enabled_folders(user_id, keep)

    def manual_unlock(self, user_id: str):
        """撤销立即锁（点错了当没发生过）：只解开关，不碰轮次/计数/加时。"""
        with self._lock:
            rule = self.settings.get_user(user_id)
            rule["manual_locked"] = False
            self.settings.save()
            backup = self._recall(user_id)
            if backup:
                self.client.set_enabled_folders(user_id, backup)

    def hard_lock(self, user_id: str) -> tuple[bool, str]:
        """立即锁：中断当前播放 + 持久锁定（轮询/重启不消失，跨天清零）。"""
        with self._lock:
            self.clear_bonus(user_id)
            rule = self.settings.get_user(user_id)
            rule["manual_locked"] = True
            self.settings.save()
            stopped, detail = self.client.stop_playback(user_id)
            self.manual_lock(user_id)  # 立刻收回媒体库
            if stopped and "没有在播" in detail:
                return True, "已锁定（当前没有在播）"
            if stopped:
                return True, f"已锁定，{detail}"
            return False, f"已锁定媒体库，但{detail}——当前集播完后无法再播"
