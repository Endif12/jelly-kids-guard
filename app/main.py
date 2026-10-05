"""Jelly Kids Guard - visual screen-time guard for Jellyfin.

No hand-written configs: server, users, libraries and per-weekday
(episode, minutes) rules are all edited in the web UI and stored in
data/settings.json. Drop-downs are fetched live from Jellyfin.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from nicegui import app, run, ui

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
rules_box = None


def refresh_all_panels():
    render_dashboard()
    render_rules()


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


def fmt_dur(secs) -> str:
    secs = int(secs or 0)
    if secs >= 3600:
        return f"{secs // 3600}时{(secs % 3600) // 60:02d}分"
    return f"{secs // 60}分{secs % 60:02d}秒"


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
    seps, smins = st.get("sess_eps", 0), st.get("sess_mins", 0)
    deps, dmins = st.get("day_eps", 0), st.get("day_mins", 0)
    day = rule["days"][str(settings.weekday())]
    max_eps = st.get("max_eps", day["max_eps"])
    soft_b = st.get("soft_b", day.get("soft_minutes", 0))
    hard_c = st.get("hard_c", day.get("hard_total", 0))
    locked = st.get("locked", False)
    with box:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"{name}（{WEEKDAYS[settings.weekday()]}）").classes("text-lg font-bold")
                badge = "播放中" if st.get("ongoing") else ("已锁定" if locked else "正常")
                ui.badge(badge, color="blue" if st.get("ongoing") and not locked
                         else ("red" if locked else "green")).set_text(badge)
            ui.label(f"本次：{seps} 集 / {fmt_dur(st.get('sess_secs', smins * 60))} ｜ "
                     f"今日：{deps} 集 / {fmt_dur(st.get('day_secs', dmins * 60))}").classes("break-words")
            ui.label(f"本次上限：{fmt_limit(max_eps)} 集 / "
                     f"B {fmt_limit(soft_b)} 分钟 / C {fmt_limit(hard_c)} 分钟 ｜ "
                     f"保障 A：{rule.get('min_minutes', 0)} 分钟").classes("break-words")
            if st.get("est_next"):
                ui.label(f"下集预判约 {st['est_next']} 分钟（{st.get('est_src', '')}）").classes("text-sm text-gray-500 break-words")
            if st.get("sess_max"):
                sess_line = f"今日已完成 {st.get('sess_used', 0)} 次 / 上限 {st['sess_max']} 次"
                if st.get("resume_at"):
                    sess_line += f"，冷却至 {st['resume_at']}"
                ui.label(sess_line).classes("text-sm text-gray-500 break-words")
            if st.get("reason"):
                ui.label(st["reason"]).classes("text-sm text-gray-500 break-words")
            if st.get("bonus_eps") or st.get("bonus_min"):
                bparts = []
                if st.get("bonus_eps"):
                    bparts.append(f"剩 {st['bonus_eps']} 集")
                if st.get("bonus_min"):
                    bparts.append(f"剩约 {st['bonus_min']} 分钟")
                ui.label("规则外加时（" + " / ".join(bparts) + "）"
                         ).classes("text-sm text-blue-600 break-words")
            playing = ""
            try:
                playing = (guard.client.now_playing() or {}).get(user_id, "")
            except Exception:  # noqa: BLE001
                pass
            if playing:
                ui.label(f"正在播放：{playing}").classes("text-sm break-words")
            with ui.row().classes("flex-wrap"):
                ui.button("+5 分钟", on_click=lambda: grant_extra(user_id, "min"))
                ui.button("+1 集", on_click=lambda: grant_extra(user_id, "eps"))
                ui.button("撤销加时", on_click=lambda: undo_extra(user_id))
                ui.button("重置今日", color="orange", on_click=lambda: ask_reset(user_id))
                ui.button("立即锁", color="red", on_click=lambda: do_manual(user_id, True))
                ui.button("解锁", color="green", on_click=lambda: do_manual(user_id, False))


def grant_extra(user_id: str, kind: str):
    if kind == "min":
        guard.grant_minutes(user_id, 5)
        ui.notify("已加 5 分钟（规则外，播完当前集前不锁）")
    else:
        guard.grant_episode(user_id)
        ui.notify("已加 1 集（规则外，下集可播）")
    guard.check_user(user_id)
    refresh_all_panels()


def undo_extra(user_id: str):
    guard.clear_bonus(user_id)
    guard.check_user(user_id)
    ui.notify("已撤销加时，规则恢复")
    refresh_all_panels()


reset_box = {"dlg": None, "uid": None}


def ask_reset(user_id: str):
    reset_box["uid"] = user_id
    if reset_box["dlg"] is not None:
        reset_box["dlg"].open()
    else:
        ui.notify("确认框未就绪，请刷新页面")


def do_reset():
    uid = reset_box["uid"]
    if reset_box["dlg"] is not None:
        reset_box["dlg"].close()
    if not uid:
        return
    ok, msg = guard.reset_today(uid)
    guard.check_user(uid)
    ui.notify(msg, color="green" if ok else "red")
    refresh_all_panels()


async def do_manual(user_id: str, lock: bool):
    if lock:
        ok, msg = await run.io_bound(guard.hard_lock, user_id)
        ui.notify(msg, color="green" if ok else "orange")
        await asyncio.sleep(2)  # 等客户端执行停止指令后再重查，徽标一次翻转到位
    else:
        await run.io_bound(guard.manual_unlock, user_id)
        ui.notify("已解锁，新的一轮开始（次数保留）")
    await run.io_bound(guard.check_user, user_id)
    render_dashboard()


def note(text: str):
    ui.label(text).classes("text-amber-800 bg-amber-100 p-3 rounded w-full")


def render_dashboard():
    if dashboard_box is None:
        return
    dashboard_box.clear()
    if not settings.is_configured():
        with dashboard_box:
            note("还没有配置 Jellyfin 服务器，请先到「服务器」页填写地址和 API Key，点保存后再点「同步用户/媒体库」。")
        return
    for uid, rule in settings.data.get("users", {}).items():
        if rule.get("enabled"):
            user_card(dashboard_box, uid)
    if not any(r.get("enabled") for r in settings.data.get("users", {}).values()):
        with dashboard_box:
            note("还没有启用任何受控用户，去「规则设置」页添加。")


# ----- pages -----------------------------------------------------------
@ui.page("/", title="Jelly Kids Guard")
def index():
    global dashboard_box, rules_box
    ui.label("Jelly Kids Guard｜儿童观影守护").classes("text-2xl font-bold break-words")
    ui.label("A保障 + B软上限 + C硬上限（下集预判），超限软锁（当前集播完，下一集打不开）。").classes("text-gray-500 break-words")

    with ui.dialog() as dlg, ui.card():
        ui.label("确定清零今日播放记录吗？").classes("font-bold break-words")
        ui.label("该用户的今日集数和分钟归零，加时同步撤销，且不可恢复。").classes("text-sm text-gray-500 break-words")
        with ui.row():
            ui.button("确认清零", color="red", on_click=do_reset)
            ui.button("取消", on_click=dlg.close)
    reset_box["dlg"] = dlg

    with ui.tabs() as tabs:
        tab_dash = ui.tab("看板")
        tab_rules = ui.tab("规则设置")
        tab_server = ui.tab("服务器")
    with ui.tab_panels(tabs, value=tab_dash).classes("w-full"):
        with ui.tab_panel(tab_dash):
            with ui.row():
                ui.button("刷新状态", on_click=manual_refresh)
                ui.button("同步用户/媒体库",
                          on_click=lambda: (ui.notify(refresh_caches()), refresh_all_panels()))
            dashboard_box = ui.column().classes("w-full")
            render_dashboard()
        with ui.tab_panel(tab_rules):
            rules_box = ui.column().classes("w-full")
            render_rules()
        with ui.tab_panel(tab_server):
            server_panel()

    ui.timer(15.0, poll_due)


_poll_state = {"last": 0.0}


async def poll_due():
    """Polling lives inside page timers (NiceGUI forbids global-scope UI).

    检查放后台线程跑，避免 Jellyfin 响应慢时卡死页面导致按钮“点了没反应”。
    """
    interval = max(0.2, float(settings.data.get("polling_minutes", 1.0) or 1.0)) * 60
    if time.time() - _poll_state["last"] >= interval:
        _poll_state["last"] = time.time()
        if settings.is_configured():
            await run.io_bound(guard.check_all)


async def manual_refresh():
    await run.io_bound(guard.check_all)
    render_dashboard()
    ui.notify("已刷新 " + time.strftime("%H:%M:%S"))


def render_rules():
    if rules_box is None:
        return
    rules_box.clear()
    if not users_cache and settings.is_configured():
        refresh_caches()
    options = dict(users_cache) or {uid: uid for uid in settings.data.get("users", {})}
    if not options:
        with rules_box:
            note(f"还没有用户数据（当前缓存：用户 {len(users_cache)} 个，媒体库 {len(libs_cache)} 个）："
                 "请先到「服务器」页保存并同步；若同步后仍是 0 个，请看容器日志里的 GET /Users 报错。")
        return
    with rules_box:
        rules_editor(options)


def rules_editor(options):
    saved = next(iter(settings.data.get("users", {}).keys()), None)
    current = {"uid": saved if saved in options else next(iter(options))}

    def editor(uid: str):
        box.clear()
        rule = settings.get_user(uid)
        with box:
            ui.label(f"用户：{users_cache.get(uid, uid)}").classes("font-bold break-words")
            ui.checkbox("启用管控", value=rule.get("enabled", True),
                        on_change=lambda e: save_field(uid, "enabled", e.value))
            ui.number("A 最低保障分钟", value=rule.get("min_minutes", 0), min=0,
                      step=5, on_change=lambda e: save_field(uid, "min_minutes", int(e.value or 0)))
            ui.label("没看够 A 不锁；0=关闭").classes("text-xs text-gray-500 break-words")
            lib_options = dict(libs_cache)
            ui.select(lib_options, multiple=True, label="超限保留媒体库",
                      value=[f for f in rule.get("keep_folders", []) if f in lib_options],
                      on_change=lambda e: save_field(uid, "keep_folders", list(e.value or []))).classes("w-full")
            ui.label("白名单：超限锁定后仍保留的库").classes("text-xs text-gray-500 break-words")
            ui.label("按星期设置每天上限（0 = 不限该项，需满足 A≤B≤C）").classes(
                "font-bold mt-2 break-words")
            ui.label("集数=最多集数 B=软上限(分) C=硬上限(分) 次数=观看次数 间隔=两次间隔(小时)").classes(
                "text-xs text-gray-500 break-words")
            with ui.grid(columns=6).classes("gap-2").style(
                    "grid-template-columns: 2.5em repeat(5, minmax(0, 1fr));"):
                for h in ["星期", "集数", "B分", "C分", "次数", "间隔h"]:
                    ui.label(h).classes("font-bold text-sm").style(
                        "white-space: normal; word-break: break-all;")
                for i, wd in enumerate(WEEKDAYS):
                    day = rule["days"][str(i)]
                    ui.label(wd).classes("text-sm").style("white-space: normal;")
                    ui.number(min=0, step=1, value=day["max_eps"],
                              on_change=lambda e, i=i: save_day(uid, i, "max_eps", int(e.value or 0))
                              ).classes("w-full").props("dense")
                    ui.number(min=0, step=5, value=day.get("soft_minutes", 0),
                              on_change=lambda e, i=i: save_day(uid, i, "soft_minutes", int(e.value or 0))
                              ).classes("w-full").props("dense")
                    ui.number(min=0, step=5, value=day.get("hard_total", 0),
                              on_change=lambda e, i=i: save_day(uid, i, "hard_total", int(e.value or 0))
                              ).classes("w-full").props("dense")
                    ui.number(min=0, step=1, value=day.get("max_sessions", 0),
                              on_change=lambda e, i=i: save_day(uid, i, "max_sessions", int(e.value or 0))
                              ).classes("w-full").props("dense")
                    ui.number(min=0, step=0.5, value=day.get("gap_hours", 0),
                              on_change=lambda e, i=i: save_day(uid, i, "gap_hours", float(e.value or 0))
                              ).classes("w-full").props("dense")
            with ui.row().classes("mt-2"):
                ui.button("保存", color="green", on_click=lambda: (settings.save(), ui.notify("已保存")))

    def save_field(uid: str, key: str, value):
        settings.get_user(uid)[key] = value
        settings.save()

    def save_day(uid: str, weekday: int, key: str, value: int):
        settings.get_user(uid)["days"][str(weekday)][key] = value
        settings.save()

    ui.select(options, label="选择要设置的用户（自动从 Jellyfin 获取）",
              value=current["uid"], on_change=lambda e: editor(e.value)).classes("w-full")
    box = ui.column().classes("w-full")
    editor(current["uid"])


def server_panel():
    srv = settings.server
    host_in = ui.input("Jellyfin 服务器地址", value=srv.get("host", "")).classes("w-full")
    ui.label("如 http://192.168.1.111:8096").classes("text-xs text-gray-500 break-words")
    token_in = ui.input("管理员 API Key", password=True, value=srv.get("token", "")).classes("w-full")
    poll_in = ui.number("轮询间隔（分钟）", value=settings.data.get("polling_minutes", 1.0),
                        min=0.2, step=0.1)
    scope_in = ui.select(COUNT_SCOPES, label="集数统计口径",
                         value=settings.data.get("count_scope", "episode")).classes("w-full")
    fallback_in = ui.number("下集预判兜底分钟",
                            value=settings.data.get("fallback_episode_minutes", 25), min=0, step=5)
    ui.label("拿不到真实下集/平均时长时使用").classes("text-xs text-gray-500 break-words")

    def save():
        settings.server["host"] = (host_in.value or "").strip().rstrip("/")
        settings.server["token"] = (token_in.value or "").strip()
        settings.data["polling_minutes"] = float(poll_in.value or 1.0)
        settings.data["count_scope"] = scope_in.value
        settings.data["fallback_episode_minutes"] = int(fallback_in.value or 0)
        settings.save()
        guard.reconnect()
        ui.notify(refresh_caches())
        refresh_all_panels()

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


if __name__ in ("__main__", "__mp_main__"):
    ui.run(host="0.0.0.0", port=8080, title="Jelly Kids Guard")
