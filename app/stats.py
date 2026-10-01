"""Playback stats via the Playback Reporting plugin (COUNT + SUM in one query)."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import requests

from jellyfin_client import JellyfinClient, normalize_user_id

logger = logging.getLogger("kids-guard")

ROW_FORMAT = "%Y-%m-%d %H:%M:%S"


class PlaybackStats:
    def __init__(self, client: JellyfinClient, item_types: list[str] | None = None):
        self.client = client
        self.item_types = list(item_types or [])
        self._json = json.JSONDecoder()

    def _where(self, user_id: str, date_start: str, date_end: str) -> str:
        uid = normalize_user_id(user_id)
        scope = ""
        if self.item_types:
            in_list = ",".join(f"'{t}'" for t in self.item_types)
            scope = f" AND ItemType IN ({in_list})"
        return (
            f"(REPLACE(LOWER(UserId), '-', '') = '{uid}' "
            f"OR UserId IN ('{user_id}', '{uid}')){scope} "
            f"AND DateCreated >= '{date_start} 00:00:00' "
            f"AND DateCreated < '{date_end} 00:00:00'"
        )

    def _query(self, sql: str):
        try:
            r = self.client._post("/user_usage_stats/submit_custom_query",
                                  {"CustomQueryString": sql})
        except requests.RequestException as exc:
            logger.error("stats query network error: %s", exc)
            return None
        if r.status_code != 200:
            logger.error("stats query HTTP %s %s", r.status_code, r.text[:200])
            return None
        try:
            return self._json.decode(r.text)
        except ValueError as exc:
            logger.error("stats decode error %s body=%s", exc, r.text[:200])
            return None

    def today(self, user_id: str, date_start: str, date_end: str) -> tuple[int, int]:
        """Return (episodes_today, minutes_today). Never raises."""
        sql = ("SELECT COUNT(*), COALESCE(SUM(PlayDuration), 0) FROM PlaybackActivity "
               f"WHERE {self._where(user_id, date_start, date_end)}")
        data = self._query(sql)
        if not isinstance(data, dict):
            return 0, 0
        try:
            rows = data.get("results", [])
            if not rows or not rows[0]:
                return 0, 0
            count = int(float(rows[0][0] or 0))
            seconds = int(float(rows[0][1] if len(rows[0]) > 1 else 0) or 0)
            return count, seconds // 60
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            logger.error("stats parse error %s", exc)
            return 0, 0

    def reset_day(self, user_id: str, date_start: str, date_end: str) -> tuple[bool, str]:
        """删除该用户今日播放记录（测试清零用，不可恢复）.

        Playback Reporting 官方前端删记录也是走 submit_custom_query 发 DELETE。
        """
        sql = (f"DELETE FROM PlaybackActivity WHERE {self._where(user_id, date_start, date_end)}")
        data = self._query(sql)
        if not isinstance(data, dict):
            return False, "删除请求失败（看日志）"
        msg = str(data.get("message", ""))
        if "error" in msg.lower():
            logger.error("reset_day SQL error: %s", msg)
            return False, f"删除失败：{msg[:200]}"
        return True, "今日记录已清零"

    def today_rows(self, user_id: str, date_start: str, date_end: str,
                   limit: int = 500) -> list[tuple[datetime, int]]:
        """Return [(start, seconds)] detail rows for session splitting."""
        sql = ("SELECT DateCreated, PlayDuration FROM PlaybackActivity "
               f"WHERE {self._where(user_id, date_start, date_end)} "
               f"ORDER BY DateCreated LIMIT {limit}")
        data = self._query(sql)
        if not isinstance(data, dict):
            return []
        rows: list[tuple[datetime, int]] = []
        for item in data.get("results", []):
            try:
                start = datetime.strptime(str(item[0])[:19], ROW_FORMAT)
                secs = int(float(item[1] or 0))
            except (ValueError, TypeError, IndexError):
                continue
            rows.append((start, secs))
        return rows
