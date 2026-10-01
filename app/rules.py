"""Lock decision engine: episode-count + watch-minutes with a minimum guarantee.

Example the owner asked for (max 2 episodes, at least 20 minutes):

* 10 min/episode -> after 2 eps (20 min) both conditions hold -> lock.
* 5 min/episode  -> after 2 eps only 10 min, guarantee not met -> allow;
  lock once 20 min is reached (soft lock lets the running episode finish).
* 21 min/episode -> after 1 ep the minutes cap is hit and guarantee met -> lock.

General rule (evaluated on every poll; the soft lock never cuts the
running stream, so "wait until the episode ends" holds automatically):

    lock = (eps >= max_eps OR mins >= max_minutes) AND mins >= min_minutes

A non-positive max_* means "unlimited" on that axis; min_minutes <= 0
disables the guarantee (pure OR behaviour).
"""

from __future__ import annotations


def should_lock(eps: int, mins: int, max_eps: int, max_minutes: int,
                min_minutes: int = 0) -> tuple[bool, str]:
    over_eps = max_eps > 0 and eps >= max_eps
    over_min = max_minutes > 0 and mins >= max_minutes
    if not (over_eps or over_min):
        return False, ""
    if min_minutes > 0 and mins < min_minutes:
        return False, f"已播 {eps} 集 / {mins} 分钟，未满最低保障 {min_minutes} 分钟，继续放行"
    if over_eps and over_min:
        return True, f"已播 {eps} 集 / {mins} 分钟，集数与时长均超限"
    if over_eps:
        return True, f"已播 {eps} 集，达到集数上限"
    return True, f"已播 {mins} 分钟，达到时长上限"


def remaining(eps: int, mins: int, max_eps: int, max_minutes: int) -> tuple[str, str]:
    eps_left = "不限" if max_eps <= 0 else str(max(0, max_eps - eps))
    min_left = "不限" if max_minutes <= 0 else str(max(0, max_minutes - mins))
    return eps_left, min_left
