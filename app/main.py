"""Jelly Kids Guard - visual screen-time guard for Jellyfin.

No hand-written configs: server, users, libraries and per-weekday
(episode, minutes) rules are all edited in the web UI and stored in
data/settings.json. Drop-downs are fetched live from Jellyfin.
"""

from __future__ import annotations

import logging
import os

from nicegui import app, ui

from locker import Guard
from store import COUNT_SCOPES, WEEKDAYS, SettingsStore

DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kids-guard")

settings = SettingsStore()
guard = Guard(settings)

users_cache: dict[str, str] = {}
libs_cache: dict[str, str] = {}
dashboard_box = None


# ----- helpers ---------------------------------------------------------
def refresh_caches() -> str:
    global users_cache, libs_cache
    if not settings.is_configured():
        return "请先填写服务器地址和 API Key"
    guard.reconnect()
    users_cache = guard.client.get_users()
    libs_cache = guard.client.get_libraries()
    msg = f"用户 {len(users_cache)} 个，媒体库 {len(libs_cache)} 个"
    if not users_cache:
        return "拉取失败：" + msg + "（检查地址/Key/网络）"
    # prune nothing; keep rules for deleted users but mark them
    return "已同步：" + msg


def fmt_limit(v: int) -> str:
    return "不限" if v <= 0 else str(v)


# ----- webhook triggers (for Jellyfin webhooks / manual curl) -----------
@app.get("/trigger/{user_id}")
def trigger_one(user_id: str):
    if not settings.is_configured():
        return {"error": "not configured"}
    try:
        st = guard.check_user(user_id)
        return {"ok": True, "locked": st["locked"], "reason": st["reason"]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@app.get("/trigger")
def trigger_all():
    guard.check_all()
    return {"ok": True}


@app.get("/api/status")
def api_status():
    return {"configured": settings.is_configured(), "status": guard.status}


# ----- dashboard -------------------------------------------------------
def user_card(box, user_id: str):
    rule = settings.get_user(user_id)
    name = users_cache.get(user_id, user_id)
    st = guard.status.get(user_id, {})
    eps, mins = st.get("eps", 0), st.get("mins", 0)
    day = rule["days"][str(settings.weekday())]
    max_eps = st.get("max_eps", day["max_eps"])
    soft_b = st.get("soft_b", day.get("soft_minutes", 0))
    hard_c = st.get("hard_c", day.get("hard_total", 0))
    locked = st.get("locked", False)
    with box:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"{name}（{WEEKDAYS[settings.weekday()]}）").classes("text-lg font-bold")
                ui.badge("已锁定", color="red" if locked else "green").set_text(
                    "已锁定" if locked else "正常")
            ui.label(f"今日：{eps} 集 / {mins} 分钟 ｜ 上限：{fmt_limit(max_eps)} 集 / "
                     f"B {fmt_limit(soft_b)} 分钟 / C {fmt_limit(hard_c)} 分钟 ｜ "
                     f"保障 A：{rule.get('min_minutes', 0)} 分钟")
            if st.get("est_next"):
                ui.label(f"下集预判约 {st['est_next']} 分钟（{st.get('est_src', '')}）").classes("text-sm text-gray-500")
            if st.get("reason"):
                ui.label(st["reason"]).classes("text-sm text-gray-500")
            playing = ""
            try:
                playing = (guard.client.now_playing() or {}).get(user_id, "")
            except Exception:  # noqa: BLE001
                pass
            if playing:
                ui.label(f"正在播放：{playing}").classes("text-sm")
            with ui.row():
                ui.button("+10 分钟", on_click=lambda: grant_bonus(user_id, 0, 10))
                ui.button("+1 集", on_click=lambda: grant_bonus(user_id, 1, 0))
                ui.button("立即锁", color="red", on_click=lambda: do_manual(user_id, True))
                ui.button("解锁", color="green", on_click=lambda: do_manual(user_id, False))


def grant_bonus(user_id: str, eps: int, mins: int):
    rule = settings.get_user(user_id)
    if rule.get("bonus_day") != settings.today_key():
        rule["bonus_eps"] = 0
        rule["bonus_minutes"] = 0
        rule["bonus_day"] = settings.today_key()
    rule["bonus_eps"] = int(rule.get("bonus_eps", 0)) + eps
    rule["bonus_minutes"] = int(rule.get("bonus_minutes", 0)) + mins
    settings.save()
    guard.check_user(user_id)
    ui.notify(f"已为今日增加 {eps} 集 / {mins} 分钟")
    render_dashboard()


def do_manual(user_id: str, lock: bool):
    (guard.manual_lock if lock else guard.manual_unlock)(user_id)
    guard.check_user(user_id)
    ui.notify("已锁定" if lock else "已解锁")
    render_dashboard()


def render_dashboard():
    if dashboard_box is None:
        return
    dashboard_box.clear()
    if not settings.is_configured():
        with dashboard_box:
            ui.alert("还没有配置 Jellyfin 服务器，请先到「服务器」页填写地址和 API Key，点保存后再点「同步用户/媒体库」。")
        return
    for uid, rule in settings.data.get("users", {}).items():
        if rule.get("enabled"):
            user_card(dashboard_box, uid)
    if not any(r.get("enabled") for r in settings.data.get("users", {}).values()):
        with dashboard_box:
            ui.alert("还没有启用任何受控用户，去「规则设置」页添加。")


# ----- pages -----------------------------------------------------------
@ui.page("/", title="Jelly Kids Guard")
def index():
    global dashboard_box
    ui.label("Jelly Kids Guard｜儿童观影守护").classes("text-2xl font-bold")
    ui.label("A保障 + B软上限 + C硬上限（下集预判），超限软锁（当前集播完，下一集打不开）。").classes("text-gray-500")

    with ui.tabs() as tabs:
        tab_dash = ui.tab("看板")
        tab_rules = ui.tab("规则设置")
        tab_server = ui.tab("服务器")
    with ui.tab_panels(tabs, value=tab_dash).classes("w-full"):
        with ui.tab_panel(tab_dash):
            with ui.row():
                ui.button("刷新状态", on_click=lambda: (guard.check_all(), render_dashboard()))
                ui.button("同步用户/媒体库", on_click=lambda: (ui.notify(refresh_caches()), render_dashboard()))
            dashboard_box = ui.column().classes("w-full")
            render_dashboard()
        with ui.tab_panel(tab_rules):
            rules_panel()
        with ui.tab_panel(tab_server):
            server_panel()

    ui.timer(30.0, lambda: guard.check_all())


def rules_panel():
    if not users_cache and settings.is_configured():
        refresh_caches()
    options = dict(users_cache) or {uid: uid for uid in settings.data.get("users", {})}
    if not options:
        ui.alert("还没有用户数据：请先到「服务器」页保存并同步。")
        return
    current = {"uid": next(iter(settings.data.get("users", {}), None)) or next(iter(options))}

    def editor(uid: str):
        box.clear()
        rule = settings.get_user(uid)
        with box:
            ui.label(f"用户：{users_cache.get(uid, uid)}").classes("font-bold")
            ui.checkbox("启用管控", value=rule.get("enabled", True),
                        on_change=lambda e: save_field(uid, "enabled", e.value))
            ui.number("A 最低保障分钟（没看够不锁，0=关闭）", value=rule.get("min_minutes", 0), min=0,
                      step=5, on_change=lambda e: save_field(uid, "min_minutes", int(e.value or 0)))
            lib_options = dict(libs_cache)
            ui.select(lib_options, multiple=True, label="超限后仍保留的媒体库（白名单）",
                      value=[f for f in rule.get("keep_folders", []) if f in lib_options],
                      on_change=lambda e: save_field(uid, "keep_folders", list(e.value or []))).classes("w-full")
            ui.label("按星期设置每天上限（0 = 不限该项，需满足 A≤B≤C）").classes("font-bold mt-2")
            with ui.grid(columns=4).classes("gap-2"):
                ui.label("星期").classes("font-bold")
                ui.label("最多集数").classes("font-bold")
                ui.label("B 软上限(分)").classes("font-bold")
                ui.label("C 硬上限(分)").classes("font-bold")
                for i, wd in enumerate(WEEKDAYS):
                    day = rule["days"][str(i)]
                    ui.label(wd)
                    ui.number(min=0, step=1, value=day["max_eps"],
                              on_change=lambda e, i=i: save_day(uid, i, "max_eps", int(e.value or 0)))
                    ui.number(min=0, step=5, value=day.get("soft_minutes", 0),
                              on_change=lambda e, i=i: save_day(uid, i, "soft_minutes", int(e.value or 0)))
                    ui.number(min=0, step=5, value=day.get("hard_total", 0),
                              on_change=lambda e, i=i: save_day(uid, i, "hard_total", int(e.value or 0)))
            with ui.row().classes("mt-2"):
                ui.button("保存", color="green", on_click=lambda: (settings.save(), ui.notify("已保存")))
                ui.button("今日临时 +10 分钟", on_click=lambda: grant_bonus(uid, 0, 10))
                ui.button("清除今日临时额度",
                          on_click=lambda: clear_bonus(uid))

    def save_field(uid: str, key: str, value):
        settings.get_user(uid)[key] = value
        settings.save()

    def save_day(uid: str, weekday: int, key: str, value: int):
        settings.get_user(uid)["days"][str(weekday)][key] = value
        settings.save()

    def clear_bonus(uid: str):
        rule = settings.get_user(uid)
        rule["bonus_eps"] = 0
        rule["bonus_minutes"] = 0
        rule["bonus_day"] = ""
        settings.save()
        ui.notify("已清除今日临时额度")

    ui.select(options, label="选择要设置的用户（自动从 Jellyfin 获取）",
              value=current["uid"], on_change=lambda e: editor(e.value)).classes("w-full")
    box = ui.column().classes("w-full")
    editor(current["uid"])


def server_panel():
    srv = settings.server
    host_in = ui.input("Jellyfin 地址（如 http://192.168.1.111:8096）", value=srv.get("host", "")).classes("w-full")
    token_in = ui.input("管理员 API Key", password=True, value=srv.get("token", "")).classes("w-full")
    poll_in = ui.number("轮询间隔（分钟）", value=settings.data.get("polling_minutes", 1.0),
                        min=0.2, step=0.1)
    scope_in = ui.select(COUNT_SCOPES, label="集数统计口径",
                         value=settings.data.get("count_scope", "episode")).classes("w-full")
    fallback_in = ui.number("下集预判兜底分钟（拿不到真实/平均时长时用）",
                            value=settings.data.get("fallback_episode_minutes", 25), min=0, step=5)

    def save():
        settings.server["host"] = (host_in.value or "").strip().rstrip("/")
        settings.server["token"] = (token_in.value or "").strip()
        settings.data["polling_minutes"] = float(poll_in.value or 1.0)
        settings.data["count_scope"] = scope_in.value
        settings.data["fallback_episode_minutes"] = int(fallback_in.value or 0)
        settings.save()
        guard.reconnect()
        ui.notify(refresh_caches())
        render_dashboard()

    def test():
        guard.reconnect()
        ok, msg = guard.client.check_connection()
        ui.notify(msg, color="green" if ok else "red")
        if ok:
            ok2, msg2 = guard.client.check_playback_reporting()
            ui.notify(msg2, color="green" if ok2 else "red")

    with ui.row():
        ui.button("保存并同步", color="green", on_click=save)
        ui.button("测试连接", on_click=test)


poll = max(0.2, float(settings.data.get("polling_minutes", 1.0) or 1.0))
ui.timer(poll * 60, lambda: guard.check_all() if settings.is_configured() else None)

ui.run(host="0.0.0.0", port=8080, title="Jelly Kids Guard")
