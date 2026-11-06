"""Plan validation and scheduling decisions. Pure functions, no I/O (ADR-002, ADR-026, ADR-029).

`decide` sees only terminal subtask results (accepted | failed | needs_human | blocked). Every
combination maps to exactly one decision, so the schedule loop can never fall through and
end the run silently. Order of precedence:

1. every subtask accepted                     -> all_accepted
2. pending subtasks that depend (transitively) on a failed/blocked one are marked `blocked`
   in the same decision; a failure never stops independent work (ADR-029)
3. subtasks whose deps are all accepted       -> dispatch (unless the wave cap is reached)
4. any subtask needs a human                  -> escalate (only once nothing else can run)
5. everything terminal, >= 1 accepted         -> partial (synthesize + final review decide
                                                 between completed_partial and failed)
6. everything terminal, nothing accepted      -> fail
7. anything else                              -> fail ("stalled"; unreachable for a valid DAG)
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping

from agentops.core.state import Plan, ScheduleDecision, Subtask, SubtaskResult

MAX_SUBTASKS = 6
ENABLED_SPECIALISTS: frozenset[str] = frozenset({"research", "analysis", "writing", "code"})


def validate_plan(
    plan: Plan,
    *,
    enabled: frozenset[str] = ENABLED_SPECIALISTS,
    max_subtasks: int = MAX_SUBTASKS,
) -> list[str]:
    """Structural checks after parsing. Returns human-readable errors (empty = valid)."""
    errors: list[str] = []
    subtasks = plan.subtasks
    if len(subtasks) > max_subtasks:
        errors.append(f"too many subtasks: {len(subtasks)} > {max_subtasks}")
    ids = [s.id for s in subtasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"duplicate subtask ids: {dupes}")
    known = set(ids)
    for s in subtasks:
        if s.specialist not in enabled:
            errors.append(
                f"subtask {s.id!r}: specialist {s.specialist!r} is not available "
                f"(available: {sorted(enabled)})"
            )
        unknown = sorted(set(s.depends_on) - known)
        if unknown:
            errors.append(f"subtask {s.id!r} depends on unknown ids {unknown}")
        if s.id in s.depends_on:
            errors.append(f"subtask {s.id!r} depends on itself")
    if not dupes:
        cycle = _find_cycle(subtasks)
        if cycle:
            errors.append(f"dependency cycle among {cycle}")
    return errors


def _find_cycle(subtasks: list[Subtask]) -> list[str]:
    """Kahn's algorithm: nodes left over after the topological sort are on (or behind) a cycle."""
    ids = {s.id for s in subtasks}
    indegree = {s.id: 0 for s in subtasks}
    children: dict[str, list[str]] = {s.id: [] for s in subtasks}
    for s in subtasks:
        for d in s.depends_on:
            if d in ids and d != s.id:
                indegree[s.id] += 1
                children[d].append(s.id)
    queue = deque(sorted(i for i, n in indegree.items() if n == 0))
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        for c in children[node]:
            indegree[c] -= 1
            if indegree[c] == 0:
                queue.append(c)
    if seen == len(subtasks):
        return []
    return sorted(i for i, n in indegree.items() if n > 0)


def max_waves(plan: Plan) -> int:
    """A valid DAG of n subtasks never needs more than n dispatch waves."""
    return len(plan.subtasks)


def decide(  # noqa: PLR0911  (one return per documented case)
    plan: Plan, results: Mapping[str, SubtaskResult], wave: int
) -> ScheduleDecision:
    by_status: dict[str, set[str]] = {
        "accepted": set(),
        "failed": set(),
        "needs_human": set(),
        "blocked": set(),
    }
    for sid, r in results.items():
        by_status[r.status].add(sid)
    accepted = by_status["accepted"]
    plan_ids = [s.id for s in plan.subtasks]

    if all(i in accepted for i in plan_ids):
        return ScheduleDecision(kind="all_accepted")

    dead = by_status["failed"] | by_status["blocked"]
    newly_blocked: dict[str, str] = {}
    for s in plan.subtasks:
        if s.id in results:
            continue
        culprits = sorted(d for d in dead if _depends_on_any(s, [d], plan))
        if culprits:
            newly_blocked[s.id] = f"depends on failed or blocked subtask(s) {culprits}"
    pending = [s for s in plan.subtasks if s.id not in results and s.id not in newly_blocked]

    ready = [s.id for s in pending if set(s.depends_on) <= accepted]
    if ready:
        if wave >= max_waves(plan):
            return ScheduleDecision(
                kind="fail",
                blocked=newly_blocked,
                reason=f"wave cap reached ({wave}/{max_waves(plan)})",
            )
        return ScheduleDecision(kind="dispatch", subtask_ids=ready, blocked=newly_blocked)

    if by_status["needs_human"]:
        return ScheduleDecision(
            kind="escalate", subtask_ids=sorted(by_status["needs_human"]), blocked=newly_blocked
        )

    if pending:
        ids = sorted(s.id for s in pending)
        return ScheduleDecision(
            kind="fail",
            subtask_ids=ids,
            blocked=newly_blocked,
            reason=f"scheduler stalled; pending: {ids}",
        )

    missing = sorted(by_status["failed"] | by_status["blocked"] | set(newly_blocked))
    if accepted:
        return ScheduleDecision(
            kind="partial",
            subtask_ids=missing,
            blocked=newly_blocked,
            reason=f"subtask(s) {missing} did not complete",
        )
    return ScheduleDecision(
        kind="fail",
        subtask_ids=missing,
        blocked=newly_blocked,
        reason=f"no subtask was accepted; failed or blocked: {missing}",
    )


def _depends_on_any(s: Subtask, targets: list[str] | set[str], plan: Plan) -> bool:
    """True if `s` depends on any target, directly or transitively."""
    by_id = {x.id: x for x in plan.subtasks}
    stack, seen = list(s.depends_on), set()
    while stack:
        d = stack.pop()
        if d in targets:
            return True
        if d in seen or d not in by_id:
            continue
        seen.add(d)
        stack.extend(by_id[d].depends_on)
    return False
