"""Lock decision engine: A (guarantee) / B (soft cap) / C (hard cap w/ lookahead).

Final spec (A <= B <= C, all minutes unless noted). A/B/C settle
**per viewing session** (each “次” resets counters); the day only
carries the session quota + cooldown:

* Episode line: sess_eps >= max_eps AND sess_mins >= A  -> lock (soft).
* Time line:    sess_mins >= B                         -> lock (soft).
* Next-episode gate C (only once something was watched in THIS
  session, sess_mins > 0): sess_mins + est_next > C -> lock NOW.
* max_eps/max_* <= 0 means unlimited on that axis; A <= 0 disables guarantee.

Examples (max 2 eps, A=20, B=40, C=60):
* 10-min eps: 2 eps = 20 min -> episode line needs A ok but B not hit ->
  keep going; lock at an episode boundary once mins >= B.
* 5-min eps: 2 eps = 10 min < A -> allow; keep going until B.
* 45-min single ep: mins >= B after it -> lock.
* 35-min ep1 (< B): next est 35 -> 35+35=70 > C=60 -> block next;
  next est 20 -> 55 <= C -> allow, lock after it (55 >= B anyway).
"""

from __future__ import annotations

from datetime import datetime, timedelta

#:，两次播放记录相隔超过该分钟数即算另一次观看（用于“今日观看次数”）。
IDLE_MINUTES = 30
#: 单次观看总时长低于该分钟数不计入“次数”（误触几分钟不算用掉一次）。
MIN_COUNTED_MINUTES = 3


def boundary_lock(eps: int, mins: int, max_eps: int, soft_b: int,
                  min_a: int = 0) -> tuple[bool, str]:
    if soft_b > 0 and mins >= soft_b:
        return True, f"已播 {mins} 分钟，达到时长上限 B"
    if max_eps > 0 and eps >= max_eps:
        if mins >= min_a:
            return True, f"已播 {eps} 集 / {mins} 分钟，达到集数上限"
        return False, f"已播 {eps} 集 / {mins} 分钟，未满最低保障 A（{min_a} 分钟），继续放行"
    return False, ""


def next_gate(mins: int, est_next: int, hard_c: int) -> tuple[bool, str]:
    if hard_c > 0 and mins > 0 and est_next > 0 and mins + est_next > hard_c:
        return True, (f"已播 {mins} 分钟，加上下集约 {est_next} 分钟将超总上限 C"
                      f"（{hard_c} 分钟），本集播完即锁")
    return False, ""


# Backwards-compatible wrapper used by tests / simple mode.
def should_lock(eps: int, mins: int, max_eps: int, max_minutes: int,
                min_minutes: int = 0) -> tuple[bool, str]:
    return boundary_lock(eps, mins, max_eps, max_minutes, min_minutes)


def remaining(eps: int, mins: int, max_eps: int, soft_b: int) -> tuple[str, str]:
    eps_left = "不限" if max_eps <= 0 else str(max(0, max_eps - eps))
    min_left = "不限" if soft_b <= 0 else str(max(0, soft_b - mins))
    return eps_left, min_left


def split_sessions(rows: list[tuple[datetime, int]],
                   idle_minutes: int = IDLE_MINUTES) -> list[tuple[datetime, datetime]]:
    """把今日播放记录按空闲间隔聚成“观看次数”，返回 [(开始, 结束)]。"""
    return [(g["start"], g["end"]) for g in clusters(rows, idle_minutes)]


def clusters(rows: list[tuple[datetime, int]],
             idle_minutes: int = IDLE_MINUTES) -> list[dict]:
    """同 split_sessions，但保留每簇原始记录，供“本次”结算用."""
    groups: list[dict] = []
    for start, secs in sorted(rows, key=lambda r: r[0]):
        end = start + timedelta(seconds=max(0, secs))
        if groups and (start - groups[-1]["end"]) <= timedelta(minutes=idle_minutes):
            g = groups[-1]
            g["rows"].append((start, secs))
            g["end"] = max(g["end"], end)
        else:
            groups.append({"start": start, "end": end, "rows": [(start, secs)]})
    return groups


def session_gate(completed: int, last_end: datetime | None, now: datetime,
                 max_sessions: int, gap_hours: float) -> tuple[bool, str, datetime | None]:
    """观看次数 + 间隔门控.

    completed: 已播完的次数（进行中的那次不算）；last_end: 上次播完时间；
    max_sessions <= 0 表示不限次数。返回 (是否锁, 原因, 可看时间)。
    """
    if max_sessions <= 0:
        return False, "", None
    if completed >= max_sessions:
        return True, f"今日已看 {completed} 次（上限 {max_sessions} 次），明天再看", None
    if gap_hours > 0 and completed >= 1 and last_end is not None:
        resume_at = last_end + timedelta(hours=gap_hours)
        if now < resume_at:
            left = resume_at - now
            hrs, rem = divmod(int(left.total_seconds()), 3600)
            desc = f"{hrs} 小时 {rem // 60} 分" if hrs else f"{rem // 60} 分"
            return True, f"冷却中，还差约 {desc}（{resume_at.strftime('%H:%M')}后可看下一次）", resume_at
    return False, "", None
