"""Tool registry: the only path from a model's tool call to a tool function (ADR-030).

invoke(call) runs, in order:
  1. lookup + allowed_agents   -> "denied" if the tool doesn't exist or isn't this agent's
  2. input validation          -> "validation_error"; the errors go back to the model as the
                                  tool result so it can self-correct (no exception)
  3. risk policy (ADR-003)     -> write outside the task / external / destructive become a
                                  ProposedAction ("proposed"); the function is NOT called
  4. Postgres token bucket     -> "rate_limited"
  5. timeout                   -> "timeout";  any exception -> "error"
  6. a tool_calls row + a tool.call event, for every outcome above

The model always gets a framed, untrusted data block back (ADR-034), never a raw string.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text

from agentops.core.state import ProposedAction
from agentops.observability import tracing
from agentops.observability.events import emit_on
from agentops.tools.framing import MAX_RESULT_CHARS, frame, sanitize, truncate
from agentops.tools.ratelimit import try_take
from agentops.tools.spec import ToolContext, ToolOutput, ToolSpec

log = logging.getLogger(__name__)

PREVIEW_CHARS = 2_000


@dataclass(frozen=True)
class Invocation:
    tool: str
    status: str  # ok | error | validation_error | denied | proposed | rate_limited | timeout
    content: str  # framed text for the model
    proposal: ProposedAction | None = None
    output: ToolOutput | None = None


def action_hash(task_id: str, tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"task": task_id, "tool": tool, "args": args}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validation_message(tool: str, exc: ValidationError) -> str:
    problems = [
        f"- {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
        for e in exc.errors(include_url=False)[:10]
    ]
    return (
        f"Invalid arguments for {tool}:\n"
        + "\n".join(problems)
        + "\nFix the arguments and call again."
    )


_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Comparable form: lower-case scheme and host, no fragment, no trailing punctuation or
    slash. The path and query stay exact (that is where exfiltrated data would go)."""
    u = url.strip().rstrip(".,;:!?")
    u = u.split("#", 1)[0]
    m = re.match(r"(?i)(https?)://([^/?]+)(.*)", u)
    if not m:
        return u
    rest = m.group(3)
    if rest in ("", "/"):
        rest = ""
    elif rest.endswith("/") and "?" not in rest:
        rest = rest[:-1]
    return f"{m.group(1).lower()}://{m.group(2).lower()}{rest}"


async def url_seen(ctx: ToolContext, url: str) -> bool:
    if not url:
        return False
    async with ctx.engine.connect() as c:
        texts = (
            (
                await c.execute(
                    text(
                        "SELECT request FROM tasks WHERE id = :t UNION ALL "
                        "SELECT coalesce(output_text, output_preview, '') FROM tool_calls "
                        "WHERE task_id = :t AND status = 'ok'"
                    ),
                    {"t": ctx.task_id},
                )
            )
            .scalars()
            .all()
        )
    want = normalize_url(url)
    return any(normalize_url(m) == want for t in texts for m in _URL.findall(t or ""))


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for s in specs:
            if s.name in self._specs:
                raise ValueError(f"duplicate tool name {s.name!r}")
            self._specs[s.name] = s

    @property
    def specs(self) -> dict[str, ToolSpec]:
        return dict(self._specs)

    def tools_for(self, agent: str) -> list[ToolSpec]:
        return sorted(
            (s for s in self._specs.values() if agent in s.allowed_agents), key=lambda s: s.name
        )

    def disabled_reason(self, name: str, settings: Any) -> str | None:
        """Why this tool can't be used in this deployment (e.g. no SMTP relay), or None."""
        spec = self._specs.get(name)
        check = spec.meta.get("disabled_reason") if spec else None
        return check(settings) if check else None

    def model_tools(self, agent: str) -> list[dict[str, Any]]:
        return [s.model_schema() for s in self.tools_for(agent)]

    async def invoke(  # noqa: PLR0911  (a ladder of guard clauses)
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        tool_call_id: str | None = None,
        rationale: str | None = None,
    ) -> Invocation:
        started = time.perf_counter()
        spec = self._specs.get(name)
        if spec is None or ctx.agent not in spec.allowed_agents:
            # Same message whether the tool exists or not: don't reveal other agents' tools.
            inv = Invocation(
                name, "denied", frame(name, "denied", f"Tool {name!r} is not available to you.")
            )
            await self._log(
                spec, name, args=args, ctx=ctx, inv=inv, started=started, tool_call_id=tool_call_id
            )
            return inv

        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as exc:
            msg = _validation_message(name, exc)
            inv = Invocation(name, "validation_error", frame(name, "validation_error", msg))
            await self._log(
                spec, name, args=args, ctx=ctx, inv=inv, started=started, tool_call_id=tool_call_id
            )
            return inv
        clean_args = parsed.model_dump(mode="json")

        if reason := self.disabled_reason(name, ctx.settings):
            # ADR-050: refused with the reason, so the specialist and the trace both say why;
            # no proposal is recorded for an action this deployment can't perform.
            msg = f"{name} refused: {reason}. Do not retry; say so in your answer."
            inv = Invocation(name, "disabled", frame(name, "disabled", msg))
            await self._log(
                spec,
                name,
                args=clean_args,
                ctx=ctx,
                inv=inv,
                started=started,
                tool_call_id=tool_call_id,
            )
            return inv

        if not spec.executes_directly:
            proposal = ProposedAction(
                hash=action_hash(str(ctx.task_id), name, clean_args),
                tool=name,
                args=clean_args,
                risk=spec.risk,  # read never reaches here (executes_directly)
                agent=ctx.agent,
                subtask_id=ctx.subtask_id,
                revision=ctx.revision,
                rationale=truncate(rationale, 1000) if rationale else None,
            )
            msg = (
                f"Not executed. {name} ({spec.risk}) affects things outside this task, so it was "
                "recorded as a proposed action for a human to approve after the task. "
                "Continue without its result."
            )
            inv = Invocation(name, "proposed", frame(name, "proposed", msg), proposal=proposal)
            await self._log(
                spec,
                name,
                args=clean_args,
                ctx=ctx,
                inv=inv,
                started=started,
                tool_call_id=tool_call_id,
            )
            return inv

        url_arg = spec.meta.get("url_provenance")
        if url_arg and not await url_seen(ctx, str(clean_args.get(url_arg, ""))):
            # Exfiltration guard (ADR-044): a read tool must not become a covert channel. Only
            # URLs that already appeared, exactly, in the request or an earlier tool result of
            # this task can be fetched, so a model can't append data to a URL.
            msg = (
                f"{name} refused: that URL did not appear in the task request or in an earlier "
                "tool result. Fetch only URLs from search results or the request, unchanged."
            )
            inv = Invocation(name, "denied", frame(name, "denied", msg))
            await self._log(
                spec,
                name,
                args=clean_args,
                ctx=ctx,
                inv=inv,
                started=started,
                tool_call_id=tool_call_id,
            )
            return inv

        key = f"{name}:{ctx.owner_id or ctx.task_id}"
        if not await try_take(ctx.engine, key, spec.rate_per_min):
            msg = f"Rate limit for {name} reached ({spec.rate_per_min}/min). Try another approach."
            inv = Invocation(name, "rate_limited", frame(name, "rate_limited", msg))
            await self._log(
                spec,
                name,
                args=clean_args,
                ctx=ctx,
                inv=inv,
                started=started,
                tool_call_id=tool_call_id,
            )
            return inv

        try:
            out = await asyncio.wait_for(spec.fn(parsed, ctx), spec.timeout_s)
        except TimeoutError:
            msg = f"{name} timed out after {spec.timeout_s:.0f}s."
            inv = Invocation(name, "timeout", frame(name, "timeout", msg))
        except Exception as exc:  # tool failures go back to the model, never crash the graph
            log.info("tool %s failed: %s", name, exc)
            msg = f"{name} failed: {type(exc).__name__}: {sanitize(str(exc))[:500]}"
            inv = Invocation(name, "error", frame(name, "error", msg))
        else:
            inv = Invocation(name, "ok", frame(name, "ok", out.text), output=out)
        await self._log(
            spec,
            name,
            args=clean_args,
            ctx=ctx,
            inv=inv,
            started=started,
            tool_call_id=tool_call_id,
        )
        return inv

    async def execute_approved(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        idempotency_key: str,
    ) -> ToolOutput:
        """Run an action a human approved (ADR-004). Only `execute_actions` calls this, after
        it inserted the side_effects row. Args are re-validated with the tool's input model
        (they may have been edited). Tools that accept an idempotency key get it (email:
        Message-ID). Raises on failure; the caller records it."""
        spec = self._specs.get(name)
        if spec is None:
            raise LookupError(f"tool {name!r} is not registered in this worker")
        parsed = spec.input_model.model_validate(args)
        started = time.perf_counter()
        kw: dict[str, Any] = {}
        if spec.meta.get("idempotency_kw"):
            kw[spec.meta["idempotency_kw"]] = idempotency_key
        try:
            out = await asyncio.wait_for(spec.fn(parsed, ctx, **kw), spec.timeout_s)
        except Exception as exc:
            inv = Invocation(name, "error", frame(name, "error", f"{type(exc).__name__}: {exc}"))
            await self._log(
                spec,
                name,
                args=parsed.model_dump(mode="json"),
                ctx=ctx,
                inv=inv,
                started=started,
                tool_call_id=idempotency_key,
            )
            raise
        inv = Invocation(name, "executed", frame(name, "ok", out.text), output=out)
        await self._log(
            spec,
            name,
            args=parsed.model_dump(mode="json"),
            ctx=ctx,
            inv=inv,
            started=started,
            tool_call_id=idempotency_key,
        )
        return out

    async def _log(
        self,
        spec: ToolSpec | None,
        name: str,
        *,
        args: dict[str, Any],
        ctx: ToolContext,
        inv: Invocation,
        started: float,
        tool_call_id: str | None,
    ) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        ok = inv.status in {"ok", "executed"}
        preview = truncate(inv.output.text, PREVIEW_CHARS) if inv.output else None
        # full (capped) output: consolidate screens memories against it (ADR-038)
        full = truncate(sanitize(inv.output.text), MAX_RESULT_CHARS) if inv.output else None
        error = None if ok or inv.status == "proposed" else truncate(inv.content, PREVIEW_CHARS)
        safe_args = json.loads(json.dumps(args, default=str))
        async with ctx.engine.begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO tool_calls (task_id, subtask_id, agent, tool, risk, args, ok, "
                    "output_preview, output_text, error, latency_ms, status, source, revision, "
                    "step, tool_call_id) VALUES (:t, :sub, :agent, :tool, :risk, "
                    "CAST(:args AS jsonb), :ok, :preview, :full, :error, :lat, :status, "
                    ":source, :rev, :step, :tcid) "
                    "RETURNING id"
                ),
                {
                    "t": ctx.task_id,
                    "sub": ctx.subtask_id,
                    "agent": ctx.agent,
                    "tool": name[:100],
                    "risk": spec.risk if spec else "unknown",
                    "args": json.dumps(safe_args),
                    "ok": ok,
                    "preview": preview,
                    "full": full,
                    "error": error,
                    "lat": latency_ms,
                    "status": inv.status,
                    "source": spec.source if spec else "native",
                    "rev": ctx.revision,
                    "step": ctx.step,
                    "tcid": tool_call_id,
                },
            )
            call_id = int(row.scalar_one())
            # Not deduplicated: every invocation really happened (or was really refused).
            event_id = await emit_on(
                conn,
                ctx.task_id,
                "tool.call",
                node="tools",
                payload={
                    "tool_call_id": call_id,
                    "tool": name,
                    "status": inv.status,
                    "agent": ctx.agent,
                    "subtask_id": ctx.subtask_id,
                    "revision": ctx.revision,
                    "step": ctx.step,
                    "latency_ms": latency_ms,
                    "proposal_hash": inv.proposal.hash if inv.proposal else None,
                },
            )
        tracing.call_span(
            "tool.call",
            latency_ms,
            {
                "agentops.task_id": str(ctx.task_id),
                "agentops.tool_call_id": call_id,
                "agentops.event_id": event_id,
                "agentops.agent": ctx.agent,
                "agentops.tool": name,
                "agentops.tool_status": inv.status,
                "agentops.subtask_id": ctx.subtask_id,
            },
            error=error if inv.status in {"error", "denied"} else None,
        )
