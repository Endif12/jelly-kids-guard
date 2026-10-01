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
        playing: dict[str, str] = {}
        for s in sessions:
            uid = s.get("UserId")
            item = s.get("NowPlayingItem") or {}
            title = item.get("Name") or item.get("SeriesName") or ""
            if uid and title:
                playing[uid] = title
        return playing
