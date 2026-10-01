"""Jellyfin server API (modern auth, works on 10.8 -> 12.x)."""

from __future__ import annotations

import json
import logging

import requests

logger = logging.getLogger("kids-guard")

APP_ID = "jelly-kids-guard"


def auth_headers(token: str) -> dict:
    return {
        "Authorization": f'MediaBrowser Client="{APP_ID}", Device="{APP_ID}", '
                         f'DeviceId="{APP_ID}", Version="1.0", Token="{token}"',
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def normalize_user_id(user_id) -> str:
    return str(user_id).replace("-", "").lower()


class JellyfinClient:
    def __init__(self, host: str, token: str, timeout: int = 15):
        self.host = (host or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.headers = auth_headers(self.token)
        self._json = json.JSONDecoder()

    def _get(self, path: str):
        r = requests.get(f"{self.host}{path}", headers=self.headers, timeout=self.timeout)
        return r

    def _post(self, path: str, payload: dict):
        r = requests.post(f"{self.host}{path}", headers=self.headers,
                          data=json.dumps(payload), timeout=self.timeout)
        return r

    def check_connection(self) -> tuple[bool, str]:
        if not self.host or not self.token:
            return False, "请先填写服务器地址和 API Key"
        try:
            r = self._get("/System/Info")
        except requests.RequestException as exc:
            return False, f"连接失败: {exc}"
        if r.status_code != 200:
            return False, f"连接失败: HTTP {r.status_code}（检查地址/API Key）"
        try:
            info = self._json.decode(r.text)
            return True, f"连接成功: {info.get('ServerName', 'Jellyfin')} {info.get('Version', '')}"
        except ValueError:
            return True, "连接成功"

    def check_playback_reporting(self) -> tuple[bool, str]:
        try:
            r = self._post("/user_usage_stats/submit_custom_query",
                           {"CustomQueryString": "SELECT 1"})
        except requests.RequestException as exc:
            return False, f"Playback Reporting 不可用: {exc}"
        if r.status_code == 200:
            return True, "Playback Reporting 可用"
        if r.status_code in (401, 403):
            return False, f"鉴权失败 HTTP {r.status_code}：检查 API Key 是否为管理员 Key"
        return False, (f"Playback Reporting 不可用: HTTP {r.status_code}。"
                       "请确认插件已安装且版本匹配（10.11 用 v17+，12.x 用 v19），"
                       "Jellyfin 大版本升级后插件也要跟着升")

    def get_users(self) -> dict[str, str]:
        """Return {user_id: username} using the server's native id format."""
        r = self._get("/Users")
        if r.status_code != 200:
            logger.error("GET /Users HTTP %s %s", r.status_code, r.text[:200])
            return {}
        try:
            return {u["Id"]: u.get("Name", u["Id"]) for u in self._json.decode(r.text)}
        except ValueError:
            return {}

    def get_libraries(self) -> dict[str, str]:
        """Return {library_id: library_name}."""
        for path in ("/Library/MediaFolders", "/Library/VirtualFolders"):
            try:
                r = self._get(path)
            except requests.RequestException as exc:
                logger.error("GET %s failed: %s", path, exc)
                continue
            if r.status_code != 200:
                continue
            try:
                items = self._json.decode(r.text)
            except ValueError:
                continue
            libs = {}
            for item in items if isinstance(items, list) else items.get("Items", []):
                lid = item.get("Id") or item.get("ItemId")
                name = item.get("Name", lid)
                if lid:
                    libs[lid] = name
            if libs:
                return libs
        return {}

    def get_policy(self, user_id: str) -> dict | None:
        try:
            r = self._get(f"/Users/{user_id}")
        except requests.RequestException as exc:
            logger.error("GET /Users/%s failed: %s", user_id, exc)
            return None
        if r.status_code != 200:
            logger.error("GET /Users/%s HTTP %s", user_id, r.status_code)
            return None
        try:
            return self._json.decode(r.text).get("Policy")
        except ValueError:
            return None

    def set_policy(self, user_id: str, policy: dict) -> bool:
        try:
            r = self._post(f"/Users/{user_id}/Policy", policy)
        except requests.RequestException as exc:
            logger.error("POST /Users/%s/Policy failed: %s", user_id, exc)
            return False
        if r.status_code != 204:
            logger.error("POST /Users/%s/Policy HTTP %s %s", user_id, r.status_code, r.text[:300])
            return False
        return True

    def get_enabled_folders(self, user_id: str) -> list:
        policy = self.get_policy(user_id)
        return list((policy or {}).get("EnabledFolders") or [])

    def set_enabled_folders(self, user_id: str, folders: list) -> bool:
        policy = self.get_policy(user_id)
        if policy is None:
            return False
        policy["EnabledFolders"] = list(folders)
        return self.set_policy(user_id, policy)

    def is_disabled(self, user_id: str) -> bool:
        policy = self.get_policy(user_id)
        return bool((policy or {}).get("IsDisabled", False))

    def now_playing(self) -> dict[str, str]:
        """Return {user_id: 'Title'} for active sessions (best effort)."""
        return {uid: d["title"] for uid, d in self.session_detail().items() if d["title"]}

    def session_detail(self) -> dict[str, dict]:
        """Return {user_id: {title, series_id, series_name}} (best effort)."""
        try:
            r = self._get("/Sessions")
        except requests.RequestException:
            return {}
        if r.status_code != 200:
            return {}
        try:
            sessions = self._json.decode(r.text)
        except ValueError:
            return {}
        detail: dict[str, dict] = {}
        for s in sessions:
            uid = s.get("UserId")
            item = s.get("NowPlayingItem") or {}
            if not uid:
                continue
            detail[uid] = {
                "title": item.get("Name") or item.get("SeriesName") or "",
                "series_id": item.get("SeriesId") or "",
                "series_name": item.get("SeriesName") or "",
            }
        return detail

    @staticmethod
    def _ticks_to_minutes(ticks) -> int:
        try:
            return int(int(ticks) // 600_000_000 // 60)
        except (TypeError, ValueError):
            return 0

    def next_episode_minutes(self, user_id: str) -> tuple[int, str]:
        """Estimate next-episode minutes: real NextUp, else series average.

        Returns (minutes, source) where source is 'next' / 'series_avg' / ''.
        Random picks can't be known upfront -> caller falls back further
        (today's average, then configured fallback).
        """
        series_id = (self.session_detail().get(user_id) or {}).get("series_id", "")
        if not series_id:
            return 0, ""
        try:
            r = self._get(f"/Shows/NextUp?userId={user_id}&seriesId={series_id}&limit=1"
                          "&disableFirstEpisode=false")
        except requests.RequestException as exc:
            logger.error("GET NextUp failed: %s", exc)
            return self.series_average_minutes(user_id, series_id)
        if r.status_code != 200:
            return self.series_average_minutes(user_id, series_id)
        try:
            items = (self._json.decode(r.text) or {}).get("Items", [])
        except ValueError:
            return self.series_average_minutes(user_id, series_id)
        if items:
            mins = self._ticks_to_minutes(items[0].get("RunTimeTicks"))
            if mins > 0:
                return mins, "next"
        return self.series_average_minutes(user_id, series_id)

    def series_average_minutes(self, user_id: str, series_id: str) -> tuple[int, str]:
        try:
            r = self._get(f"/Users/{user_id}/Items?ParentId={series_id}&Recursive=true"
                          "&IncludeItemTypes=Episode&Fields=RunTimeTicks&Limit=200")
        except requests.RequestException as exc:
            logger.error("GET series episodes failed: %s", exc)
            return 0, ""
        if r.status_code != 200:
            return 0, ""
        try:
            items = (self._json.decode(r.text) or {}).get("Items", [])
        except ValueError:
            return 0, ""
        durs = [self._ticks_to_minutes(i.get("RunTimeTicks")) for i in items]
        durs = [d for d in durs if d > 0]
        if not durs:
            return 0, ""
        return sum(durs) // len(durs), "series_avg"
