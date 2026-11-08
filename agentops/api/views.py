"""Read models for the UI (Phase 5): task detail, trace timeline, cost and time aggregates.

Everything here is derived from rows the run already writes (tasks, task_budgets, events,
llm_calls, tool_calls, approvals, side_effects) plus the task's latest checkpoint. Nothing is
stored twice. Every payload that can carry model/tool text goes through `redact` (ADR-042).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.db.models import Task
from agentops.observability.redact import redact, truncate
from agentops.settings import Settings

TERMINAL = frozenset(
    {"completed", "completed_partial", "failed", "rejected", "budget_exceeded", "cancelled"}
)
PAUSED = frozenset({"awaiting_approval"})


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dump(v: Any) -> Any:
    return v.model_dump(mode="json") if hasattr(v, "model_dump") else v


# ---- checkpoint state ----------------------------------------------------------------------
async def graph_values(graph: CompiledStateGraph[Any], task_id: uuid.UUID) -> dict[str, Any]:
    snap = await graph.aget_state({"configurable": {"thread_id": str(task_id)}})
    return dict(snap.values or {})


# ---- Mailpit link for a sent email --------------------------------------------------------
async def mailpit_link(settings: Settings, idempotency_key: str) -> str:
    """Deep link to the message in Mailpit's UI. Message-ID = <idempotency key>@agentops
    (ADR-004). Resolves Mailpit's internal id; falls back to a search link."""
    query = f"message-id:{idempotency_key}@agentops"
    ui = settings.mailpit_ui_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(
                f"{settings.mailpit_api_url.rstrip('/')}/api/v1/search", params={"query": query}
            )
            r.raise_for_status()
            msgs = r.json().get("messages") or []
        if msgs:
            return f"{ui}/view/{msgs[0]['ID']}"
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return f"{ui}/search?q={httpx.QueryParams({'q': query})['q']}"


# ---- task detail ---------------------------------------------------------------------------
async def _rows(engine: AsyncEngine, sql: str, **p: Any) -> list[dict[str, Any]]:
    async with engine.connect() as c:
        return [dict(r._mapping) for r in await c.execute(text(sql), p)]


async def _one(engine: AsyncEngine, sql: str, **p: Any) -> dict[str, Any] | None:
    rows = await _rows(engine, sql, **p)
    return rows[0] if rows else None


def _subtask_activity(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Live per-subtask status from events, for subtasks without a terminal result yet."""
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        p = e["payload"] or {}
        sid = p.get("subtask_id")
        if not sid:
            continue
        cur = out.setdefault(sid, {"status": "pending", "revision": 0, "last_score": None})
        t = e["type"]
        if t == "subtask.dispatched":
            cur["status"] = "dispatched"
        elif t == "node.started" and e["node"] == "specialist":
            cur["status"] = "working"
        elif t == "node.completed" and e["node"] == "specialist":
            cur["status"] = "in_review"
        elif t == "review.verdict":
            cur["last_score"] = p.get("score")
            cur["revision"] = p.get("revision", 0)
            cur["status"] = "revising" if p.get("verdict") == "revise" else "in_review"
        elif t == "subtask.finished":
            cur["status"] = p.get("status", cur["status"])
    return out


async def task_detail(
    engine: AsyncEngine, graph: CompiledStateGraph[Any] | None, task: Task, settings: Settings
) -> dict[str, Any]:
    tid = task.id
    values = await graph_values(graph, tid) if graph is not None else {}
    events = await _rows(
        engine,
        "SELECT id, type, node, payload FROM events WHERE task_id=:t AND type IN "
        "('subtask.dispatched','node.started','node.completed','review.verdict',"
        "'subtask.finished') ORDER BY id",
        t=tid,
    )
    activity = _subtask_activity(events)
    approvals = await _rows(
        engine,
        "SELECT id, level AS kind, key, status::text AS status, version, payload, decision, "
        "source, blocking, created_at, decided_at, expires_at FROM approvals "
        "WHERE task_id=:t ORDER BY created_at",
        t=tid,
    )
    side_effects = {
        r["action_hash"]: r
        for r in await _rows(
            engine,
            "SELECT idempotency_key, action_hash, status::text AS status, tool, error "
            "FROM side_effects WHERE task_id=:t",
            t=tid,
        )
    }
    budget = await _one(
        engine,
        "SELECT max_usd, max_tokens, max_calls, used_usd, used_tokens, used_calls "
        "FROM task_budgets WHERE task_id=:t",
        t=tid,
    )

    plan = values.get("plan")
    results = values.get("results") or {}
    reviews = values.get("reviews") or []
    decision = values.get("decision")
    blocked = dict(decision.blocked) if decision is not None else {}
    subtasks = []
    for st in plan.subtasks if plan is not None else []:
        res = results.get(st.id)
        act = activity.get(st.id, {})
        status = res.status if res is not None else act.get("status", "pending")
        if res is None and st.id in blocked:
            status = "blocked"
        subtasks.append(
            {
                "id": st.id,
                "description": st.description,
                "specialist": st.specialist,
                "depends_on": list(st.depends_on),
                "expected_output": st.expected_output,
                "complexity": st.complexity,
                "status": status,
                "revisions": res.revisions if res is not None else act.get("revision", 0),
                "output": truncate(res.output, 50_000) if res and res.output else None,
                "reason": (res.reason if res is not None else None) or blocked.get(st.id),
                "human_override": bool(res.human_override) if res is not None else False,
                "reviews": [
                    {"revision": r.revision, **r.verdict.model_dump(mode="json")}
                    for r in reviews
                    if r.subtask_id == st.id
                ],
            }
        )

    by_key = {a["key"]: a for a in approvals}
    decisions = values.get("action_decisions") or {}
    outcomes = values.get("action_outcomes") or {}
    actions = []
    for h, a in (values.get("proposed_actions") or {}).items():
        appr = by_key.get(f"action:{h}")
        out = outcomes.get(h)
        se = side_effects.get(h)
        link = None
        if a.tool == "send_email" and se is not None and se["status"] in {"done", "in_progress"}:
            link = await mailpit_link(settings, se["idempotency_key"])
        actions.append(
            {
                "hash": h,
                "tool": a.tool,
                "risk": a.risk,
                "agent": a.agent,
                "subtask_id": a.subtask_id,
                "args": redact(a.args),
                "rationale": a.rationale,
                "approval": (
                    {
                        "id": str(appr["id"]),
                        "status": appr["status"],
                        "version": appr["version"],
                        "decision": redact(appr["decision"]),
                    }
                    if appr
                    else None
                ),
                "decision": decisions[h].decision.action if h in decisions else None,
                "outcome": _dump(out) if out is not None else None,
                "side_effect_status": se["status"] if se else None,
                "mailpit_url": link,
            }
        )

    outcome = values.get("outcome")
    return {
        "id": str(tid),
        "request": task.request,
        "graph": task.graph,
        "status": task.status.value,
        "kind": task.kind,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "root_task_id": str(task.root_task_id) if task.root_task_id else None,
        "fork_checkpoint_id": task.fork_checkpoint_id,
        "scenario": (task.config or {}).get("scenario"),
        "error": task.error,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "budget": {k: float(v) for k, v in budget.items()} if budget else None,
        "plan": (
            {"confidence": plan.confidence, "rationale": plan.rationale}
            if plan is not None
            else None
        ),
        "plan_confidence": _dump(values.get("plan_confidence")),
        "memories": [_dump(m) for m in values.get("memories") or []],
        "subtasks": subtasks,
        "actions": actions,
        "approvals": [
            {
                **{k: v for k, v in a.items() if k not in {"payload", "decision"}},
                "id": str(a["id"]),
                "created_at": _iso(a["created_at"]),
                "decided_at": _iso(a["decided_at"]),
                "expires_at": _iso(a["expires_at"]),
            }
            for a in approvals
        ],
        "final": values.get("final") or (task.result or {}).get("final"),
        "final_review": _dump(values.get("final_review")),
        "partial": bool(values.get("partial")),
        "outcome": _dump(outcome),
        "failure_reason": values.get("failure_reason"),
        "explanation": explanation(task.status.value, values, task.error),
    }


def explanation(status: str, values: dict[str, Any], error: str | None) -> str | None:
    """Plain-language reason for a non-clean ending (partial / failed / rejected / budget)."""
    outcome = values.get("outcome")
    reason = (outcome.reason if outcome is not None else None) or error
    if status == "completed_partial":
        missing = [
            sid for sid, r in (values.get("results") or {}).items() if r.status != "accepted"
        ]
        unperformed = [
            o.tool
            for o in (values.get("action_outcomes") or {}).values()
            if o.status not in {"done", "human_done"}
        ]
        parts = []
        if missing:
            parts.append(f"subtasks not accepted: {', '.join(sorted(missing))}")
        if unperformed:
            parts.append(f"actions not performed: {', '.join(unperformed)}")
        return "; ".join(parts) or reason or "partial deliverable"
    if status in {"failed", "rejected", "budget_exceeded", "cancelled"}:
        return reason or status.replace("_", " ")
    return None


# ---- trace timeline ------------------------------------------------------------------------
TRACE_KINDS = {"llm": "llm.call", "tool": "tool.call"}


async def trace_page(
    engine: AsyncEngine,
    task_id: uuid.UUID,
    *,
    after: int,
    limit: int,
    kinds: set[str] | None,
    agent: str | None,
    errors_only: bool,
) -> dict[str, Any]:
    """One ordered timeline (event ids are monotonic). llm.call / tool.call events are joined
    with their ledger rows; all text is redacted and truncated."""
    clauses = ["e.task_id = :t", "e.id > :after"]
    params: dict[str, Any] = {"t": task_id, "after": after, "lim": limit + 1}
    if kinds:
        types = [TRACE_KINDS[k] for k in kinds if k in TRACE_KINDS]
        filters = []
        if types:
            filters.append("e.type = ANY(:types)")
            params["types"] = types
        if "event" in kinds:
            filters.append("e.type <> ALL(:calltypes)")
            params["calltypes"] = list(TRACE_KINDS.values())
        clauses.append("(" + " OR ".join(filters or ["FALSE"]) + ")")
    if agent:
        clauses.append(
            "(l.role = :agent OR tc.agent = :agent OR e.payload->>'role' = :agent "
            "OR e.payload->>'agent' = :agent)"
        )
        params["agent"] = agent
    if errors_only:
        clauses.append(
            "(l.ok IS FALSE OR tc.status IN ('error','denied') OR e.type LIKE '%.failed' "
            "OR (e.type = 'task.finished' AND e.payload->>'status' <> 'completed') "
            "OR l.attempt > 1)"
        )
    # fixed SQL fragments only; every value is a bound parameter
    sql = (
        "SELECT e.id, e.type, e.node, e.payload, e.created_at, "  # noqa: S608
        "l.id AS l_id, l.role, l.purpose, l.provider, l.model, l.attempt, l.ok, "
        "l.error AS l_error, "
        "l.input_tokens, l.output_tokens, l.cost_usd, l.latency_ms AS l_latency, l.response, "
        "tc.id AS tc_id, tc.agent, tc.tool, tc.risk, tc.args, tc.status AS tc_status, "
        "tc.output_preview, tc.error AS tc_error, tc.latency_ms AS tc_latency, tc.subtask_id, "
        "tc.revision, tc.step "
        "FROM events e "
        "LEFT JOIN llm_calls l ON e.type = 'llm.call' AND l.task_id = e.task_id "
        "  AND l.id = (e.payload->>'llm_call_id')::bigint "
        "LEFT JOIN tool_calls tc ON e.type = 'tool.call' AND tc.task_id = e.task_id "
        "  AND tc.id = (e.payload->>'tool_call_id')::bigint "
        f"WHERE {' AND '.join(clauses)} ORDER BY e.id LIMIT :lim"
    )
    rows = await _rows(engine, sql, **params)
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        item: dict[str, Any] = {
            "id": r["id"],
            "type": r["type"],
            "node": r["node"],
            "at": _iso(r["created_at"]),
            "kind": "event",
        }
        if r["l_id"] is not None:
            item |= {
                "kind": "llm",
                "llm_call_id": r["l_id"],
                "agent": r["role"],
                "purpose": r["purpose"],
                "model": f"{r['provider']}/{r['model']}",
                "attempt": r["attempt"],
                "ok": r["ok"],
                "retry": r["attempt"] > 1,
                "error": redact(r["l_error"]) if r["l_error"] else None,
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cost_usd": r["cost_usd"],
                "latency_ms": r["l_latency"],
                "output": redact(r["response"], max_chars=1000),
            }
        elif r["tc_id"] is not None:
            item |= {
                "kind": "tool",
                "tool_call_id": r["tc_id"],
                "agent": r["agent"],
                "tool": r["tool"],
                "risk": r["risk"],
                "status": r["tc_status"],
                "ok": r["tc_status"] not in {"error", "denied"},
                "subtask_id": r["subtask_id"],
                "revision": r["revision"],
                "step": r["step"],
                "input": redact(r["args"], max_chars=500),
                "output": redact(r["output_preview"], max_chars=1000),
                "error": redact(r["tc_error"]) if r["tc_error"] else None,
                "latency_ms": r["tc_latency"],
            }
        else:
            item["payload"] = redact(r["payload"], max_chars=500)
        items.append(item)
    return {"items": items, "next_cursor": items[-1]["id"] if more and items else None}


# ---- cost and time -------------------------------------------------------------------------
Interval = tuple[datetime, datetime | None]


def _intervals(
    status_events: list[dict[str, Any]],
) -> tuple[list[Interval], list[Interval], datetime | None]:
    """(compute intervals, human-wait intervals, finished_at); an open interval ends None."""
    compute: list[tuple[datetime, datetime | None]] = []
    waits: list[tuple[datetime, datetime | None]] = []
    seg: datetime | None = None
    wait: datetime | None = None
    finished: datetime | None = None
    for e in status_events:
        t, at, st = e["type"], e["created_at"], (e["payload"] or {}).get("status")
        closes_segment = t in {"run.segment_started", "task.finished"} or (
            t == "task.status" and st == "awaiting_approval"
        )
        if closes_segment and seg is not None:
            compute.append((seg, at))
            seg = None
        closes_wait = t == "task.finished" or (t == "task.status" and st == "running")
        if closes_wait and wait is not None:
            waits.append((wait, at))
            wait = None
        if t == "run.segment_started":
            seg = at
        elif t == "task.status" and st == "awaiting_approval":
            wait = wait or at
        elif t == "task.finished":
            finished = at
    if seg is not None:
        compute.append((seg, None))
    if wait is not None:
        waits.append((wait, None))
    return compute, waits, finished


def time_breakdown(
    status_events: list[dict[str, Any]], created_at: datetime, now: datetime, terminal: bool
) -> dict[str, float]:
    """Compute vs human-wait vs queue time from the event log.

    compute: each run segment, from `run.segment_started` to the next pause / finish /
             segment start (a crashed segment ends where its successor begins)
    human wait: from `task.status=awaiting_approval` to the next `task.status=running`
             (or now, if still waiting)
    queue: the rest of wall time (waiting for a worker / lease expiry)."""
    compute, waits, finished = _intervals(status_events)
    open_end = now if not terminal else (finished or now)

    def total(iv: list[tuple[datetime, datetime | None]]) -> float:
        return sum(((b or open_end) - a).total_seconds() for a, b in iv)

    end = finished if terminal and finished else now
    wall = max(0.0, (end - created_at).total_seconds())
    c, h = total(compute), total(waits)
    return {
        "wall_s": round(wall, 3),
        "compute_s": round(c, 3),
        "human_wait_s": round(h, 3),
        "queue_s": round(max(0.0, wall - c - h), 3),
    }


async def cost_summary(engine: AsyncEngine, task: Task) -> dict[str, Any]:
    tid = task.id
    calls = await _rows(
        engine,
        "SELECT role, provider, model, ok, attempt, input_tokens, output_tokens, cost_usd, "
        "latency_ms FROM llm_calls WHERE task_id=:t ORDER BY id",
        t=tid,
    )
    tools = await _rows(
        engine,
        "SELECT agent, tool, status, latency_ms FROM tool_calls WHERE task_id=:t",
        t=tid,
    )
    status_events = await _rows(
        engine,
        "SELECT type, payload, created_at FROM events WHERE task_id=:t AND type IN "
        "('run.segment_started','task.status','task.finished') ORDER BY id",
        t=tid,
    )
    approvals = await _rows(
        engine,
        "SELECT level, key, status::text AS status, blocking, created_at, decided_at "
        "FROM approvals WHERE task_id=:t ORDER BY created_at",
        t=tid,
    )
    budget = await _one(
        engine,
        "SELECT max_usd, max_tokens, max_calls, used_usd, used_tokens, used_calls, "
        "unpriced_calls FROM task_budgets WHERE task_id=:t",
        t=tid,
    )

    def agg() -> dict[str, Any]:
        return {
            "calls": 0,
            "errors": 0,
            "retries": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
        }

    total = agg()
    by_agent: dict[str, dict[str, Any]] = defaultdict(agg)
    by_model: dict[str, dict[str, Any]] = defaultdict(agg)
    for c in calls:
        for bucket in (total, by_agent[c["role"]], by_model[f"{c['provider']}/{c['model']}"]):
            bucket["calls"] += 1
            bucket["errors"] += 0 if c["ok"] else 1
            bucket["retries"] += 1 if c["attempt"] > 1 else 0
            bucket["input_tokens"] += c["input_tokens"]
            bucket["output_tokens"] += c["output_tokens"]
            bucket["cost_usd"] += float(c["cost_usd"] or 0)
            bucket["latency_ms"] += c["latency_ms"]
    tool_agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "latency_ms": 0}
    )
    for t in tools:
        b = tool_agg[t["tool"]]
        b["calls"] += 1
        b["errors"] += 1 if t["status"] in {"error", "denied"} else 0
        b["latency_ms"] += t["latency_ms"]

    now = datetime.now(UTC)
    status = task.status.value
    return {
        "task_id": str(tid),
        "total": _round(total),
        "by_agent": {k: _round(v) for k, v in sorted(by_agent.items())},
        "by_model": {k: _round(v) for k, v in sorted(by_model.items())},
        "tools": dict(sorted(tool_agg.items())),
        "budget": {k: float(v) for k, v in budget.items()} if budget else None,
        "time": time_breakdown(status_events, task.created_at, now, status in TERMINAL),
        "approvals": [
            {
                "kind": a["level"],
                "key": a["key"],
                "status": a["status"],
                "blocking": a["blocking"],
                "wait_s": round(((a["decided_at"] or now) - a["created_at"]).total_seconds(), 3),
            }
            for a in approvals
        ],
    }


def _round(b: dict[str, Any]) -> dict[str, Any]:
    return {**b, "cost_usd": round(b["cost_usd"], 6)}
