"""Lock decision engine: A (guarantee) / B (soft cap) / C (hard cap w/ lookahead).

Final spec (A <= B <= C, all minutes unless noted):

* Episode line: eps >= max_eps AND mins >= A  -> lock (soft).
* Time line:    mins >= B                    -> lock (soft, whatever eps is;
                e.g. a single 45-min episode locks right after it ends).
* Next-episode gate C (only once something was watched today, mins > 0):
  est = estimated next-episode minutes (real NextUp > series average >
        today's average > fallback). If mins + est > C -> lock NOW
        (soft lock never cuts the running episode; the next one won't open).
        Else allow the next episode.
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
