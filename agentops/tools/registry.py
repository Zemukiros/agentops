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
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text

from agentops.core.state import ProposedAction
from agentops.observability.events import emit_on
from agentops.tools.framing import frame, sanitize, truncate
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

    def model_tools(self, agent: str) -> list[dict[str, Any]]:
        return [s.model_schema() for s in self.tools_for(agent)]

    async def invoke(
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
        ok = inv.status == "ok"
        preview = truncate(inv.output.text, PREVIEW_CHARS) if inv.output else None
        error = None if ok or inv.status == "proposed" else truncate(inv.content, PREVIEW_CHARS)
        safe_args = json.loads(json.dumps(args, default=str))
        async with ctx.engine.begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO tool_calls (task_id, subtask_id, agent, tool, risk, args, ok, "
                    "output_preview, error, latency_ms, status, source, revision, step, "
                    "tool_call_id) VALUES (:t, :sub, :agent, :tool, :risk, CAST(:args AS jsonb), "
                    ":ok, :preview, :error, :lat, :status, :source, :rev, :step, :tcid) "
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
            await emit_on(
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
