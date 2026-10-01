"""Playback stats via the Playback Reporting plugin (COUNT + SUM in one query)."""

from __future__ import annotations

import json
import logging

import requests

from jellyfin_client import JellyfinClient, normalize_user_id

logger = logging.getLogger("kids-guard")


class PlaybackStats:
    def __init__(self, client: JellyfinClient, item_types: list[str] | None = None):
        self.client = client
        self.item_types = list(item_types or [])
        self._json = json.JSONDecoder()

    def today(self, user_id: str, date_start: str, date_end: str) -> tuple[int, int]:
        """Return (episodes_today, minutes_today). Never raises."""
        uid = normalize_user_id(user_id)
        scope = ""
        if self.item_types:
            in_list = ",".join(f"'{t}'" for t in self.item_types)
            scope = f" AND ItemType IN ({in_list})"
        sql = (
            "SELECT COUNT(*), COALESCE(SUM(PlayDuration), 0) FROM PlaybackActivity "
            f"WHERE (REPLACE(LOWER(UserId), '-', '') = '{uid}' "
            f"OR UserId IN ('{user_id}', '{uid}')){scope} "
            f"AND DateCreated >= '{date_start} 00:00:00' "
            f"AND DateCreated < '{date_end} 00:00:00'"
        )
        try:
            r = self.client._post("/user_usage_stats/submit_custom_query",
                                  {"CustomQueryString": sql})
        except requests.RequestException as exc:
            logger.error("stats query network error: %s", exc)
            return 0, 0
        if r.status_code != 200:
            logger.error("stats query HTTP %s %s", r.status_code, r.text[:200])
            return 0, 0
        try:
            data = self._json.decode(r.text)
            rows = data.get("results", [])
            if not rows or not rows[0]:
                return 0, 0
            count = int(float(rows[0][0] or 0))
            seconds = int(float(rows[0][1] if len(rows[0]) > 1 else 0) or 0)
            return count, seconds // 60
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            logger.error("stats parse error %s body=%s", exc, r.text[:200])
            return 0, 0
