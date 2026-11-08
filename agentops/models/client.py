"""ModelClient: the only path from agents to a chat model (ADR-009, ADR-024).

Per attempt (including retries and structured-output re-asks):
  1. count input tokens exactly; worst case = input + max_output_tokens, priced
  2. RESERVE the worst case in the task's budget ledger (raises BudgetExceededError, no call)
  3. call the model
  4. record an `llm_calls` row + an `llm.call` event: tokens, cost, latency, ok/error
  5. SETTLE the reservation with actual usage

If the attempt is cancelled (lease lost, shutdown) between 2 and 5, the reservation is left
open and the next run segment charges it at its worst case (BudgetLedger.charge_orphans).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentops.core.budget import BudgetLedger
from agentops.db.models import LLMCall
from agentops.models import pricing
from agentops.models.fake import REASK, FakeScript, ScriptedChatModel
from agentops.observability import tracing
from agentops.observability.events import emit_on
from agentops.settings import LLMMode, RoleModel, Settings

BUDGET_WARNING_FRACTION = 0.8


class ChatFactory(Protocol):
    def __call__(self, role: str, purpose: str, cfg: RoleModel) -> BaseChatModel: ...


class ModelCallError(Exception):
    """All attempts failed."""


class ModelConfigError(RuntimeError):
    """The configured model can't be used safely (e.g. no verified price in real mode)."""


@dataclass(frozen=True)
class ModelResult:
    message: AIMessage
    parsed: BaseModel | None
    attempts: int
    tokens: int  # all attempts
    usd: float | None  # all attempts; None if any attempt was unpriced


@dataclass(frozen=True)
class _Attempt:
    raw: AIMessage | None
    parsed: BaseModel | None
    error: str | None
    tokens: int
    usd: float | None


def fake_factory(script: FakeScript | None = None, scenario: str = "default") -> ChatFactory:
    def make(role: str, purpose: str, cfg: RoleModel) -> BaseChatModel:
        return ScriptedChatModel(
            role=role,
            purpose=purpose,
            scenario=scenario,
            script=script or FakeScript(),
            model_name=cfg.model,
            max_tokens=cfg.max_output_tokens,
        )

    return make


def _digest(messages: Sequence[BaseMessage]) -> str:
    raw = json.dumps([[m.type, m.text] for m in messages], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class ModelClient:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        chat_factory: ChatFactory | None = None,
        *,
        ledger: BudgetLedger | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if chat_factory is None:
            if settings.llm_mode is not LLMMode.FAKE:
                raise ModelConfigError("LLM_MODE=real needs an explicit chat factory")
            chat_factory = fake_factory()
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._factory = chat_factory
        self._ledger = ledger or BudgetLedger(sessionmaker)
        self._sleep = sleep

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def _price_or_refuse(self, cfg: RoleModel) -> None:
        price = pricing.lookup(cfg.provider, cfg.model)
        if self._settings.llm_mode is LLMMode.REAL and (price is None or not price.priced):
            raise ModelConfigError(
                f"no verified price for {cfg.provider}/{cfg.model}; refusing a real call "
                "because its worst-case cost can't be reserved"
            )

    async def invoke(
        self,
        role: str,
        messages: Sequence[BaseMessage],
        *,
        task_id: uuid.UUID,
        purpose: str = "default",
        tools: Sequence[Any] | None = None,
        schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        cfg = self._settings.model_for(role)
        self._price_or_refuse(cfg)
        model = self._factory(role, purpose, cfg)
        convo = list(messages)
        max_attempts = 1 + self._settings.llm_max_retries
        last_error: str | None = None
        total_tokens = 0
        total_usd: float | None = 0.0

        for attempt in range(1, max_attempts + 1):
            a = await self._attempt(
                model,
                cfg,
                convo,
                task_id=task_id,
                role=role,
                purpose=purpose,
                attempt=attempt,
                tools=tools,
                schema=schema,
            )
            total_tokens += a.tokens
            total_usd = None if total_usd is None or a.usd is None else total_usd + a.usd
            if a.error is None and a.raw is not None:
                return ModelResult(
                    message=a.raw,
                    parsed=a.parsed,
                    attempts=attempt,
                    tokens=total_tokens,
                    usd=total_usd,
                )
            last_error = a.error
            if attempt < max_attempts:
                if schema is not None and a.raw is not None:
                    # structured-output re-ask: show the model its invalid answer and the error
                    convo = [
                        *messages,
                        a.raw,
                        HumanMessage(
                            f"Your previous answer was invalid: {a.error}. Answer again.",
                            name=REASK,
                        ),
                    ]
                await self._sleep(self._settings.llm_retry_base_seconds * 2 ** (attempt - 1))

        raise ModelCallError(f"{role}: all {max_attempts} attempts failed; last: {last_error}")

    async def _attempt(
        self,
        model: BaseChatModel,
        cfg: RoleModel,
        convo: list[BaseMessage],
        *,
        task_id: uuid.UUID,
        role: str,
        purpose: str,
        attempt: int,
        tools: Sequence[Any] | None,
        schema: type[BaseModel] | None,
    ) -> _Attempt:
        """reserve -> call -> record -> settle, for one attempt."""
        runnable: Any
        bound_tools: list[Any] | None
        if schema is not None:
            runnable = model.with_structured_output(schema, include_raw=True, method="json_schema")
            bound_tools = [schema]
        elif tools:
            runnable = model.bind_tools(list(tools))
            bound_tools = list(tools)
        else:
            runnable, bound_tools = model, None

        # Worst case for this attempt: exact input tokens + the hard output cap.
        in_est = await asyncio.to_thread(model.get_num_tokens_from_messages, convo, bound_tools)
        worst_usd = pricing.cost_usd(cfg.provider, cfg.model, in_est, cfg.max_output_tokens)
        reservation = await self._ledger.reserve(
            task_id, usd=worst_usd or 0.0, tokens=in_est + cfg.max_output_tokens
        )

        started = time.perf_counter()
        raw: AIMessage | None = None
        parsed: BaseModel | None = None
        error: str | None = None
        try:
            out = await runnable.ainvoke(convo)
            if schema is not None:
                raw, parsed = out["raw"], out["parsed"]
                if out.get("parsing_error") is not None or parsed is None:
                    error = f"structured output invalid: {out.get('parsing_error')}"
            else:
                raw = out
        except Exception as exc:  # provider/transport errors are retried
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)

        usage: dict[str, Any] = dict(raw.usage_metadata or {}) if raw is not None else {}
        in_tok = int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        cost = pricing.cost_usd(cfg.provider, cfg.model, in_tok, out_tok)
        await self._record(
            task_id=task_id,
            role=role,
            purpose=purpose,
            cfg=cfg,
            attempt=attempt,
            ok=error is None,
            error=error,
            in_tok=in_tok,
            out_tok=out_tok,
            cost=cost,
            reserved_usd=reservation.usd,
            reservation_id=reservation.id,
            latency_ms=latency_ms,
            digest=_digest(convo),
            response=raw,
        )
        await self._ledger.settle(reservation, usd=cost, tokens=in_tok + out_tok)
        await self._warn_if_near_limit(task_id)
        return _Attempt(raw=raw, parsed=parsed, error=error, tokens=in_tok + out_tok, usd=cost)

    async def _warn_if_near_limit(self, task_id: uuid.UUID) -> None:
        """One `budget.warning` event per task when spend first reaches 80% of any limit."""
        snap = await self._ledger.snapshot(task_id)
        if snap.fraction_used < BUDGET_WARNING_FRACTION:
            return
        async with self._sessionmaker() as s, s.begin():
            await emit_on(
                await s.connection(),
                task_id,
                "budget.warning",
                payload={
                    "fraction_used": round(snap.fraction_used, 4),
                    "usd_used": snap.usd_used,
                    "max_usd": snap.max_usd,
                    "llm_calls": snap.llm_calls,
                    "max_llm_calls": snap.max_llm_calls,
                    "tokens_used": snap.tokens_used,
                    "max_tokens": snap.max_tokens,
                },
                dedupe_key="budget:warning:80",
            )

    async def _record(
        self,
        *,
        task_id: uuid.UUID,
        role: str,
        purpose: str,
        cfg: RoleModel,
        attempt: int,
        ok: bool,
        error: str | None,
        in_tok: int,
        out_tok: int,
        cost: float | None,
        reserved_usd: float,
        reservation_id: int,
        latency_ms: int,
        digest: str,
        response: AIMessage | None,
    ) -> None:
        resp = None
        if response is not None:
            resp = {"content": response.text, "tool_calls": response.tool_calls}
        async with self._sessionmaker() as s, s.begin():
            call = LLMCall(
                task_id=task_id,
                role=role,
                purpose=purpose,
                provider=cfg.provider,
                model=cfg.model,
                attempt=attempt,
                ok=ok,
                error=error[:2000] if error else None,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                price_table_version=pricing.PRICE_TABLE_VERSION,
                latency_ms=latency_ms,
                request_digest=digest,
                response=resp,
                reservation_id=reservation_id,
            )
            s.add(call)
            await s.flush()
            # Not deduplicated: every attempt is a real (possibly billed) call.
            event_id = await emit_on(
                await s.connection(),
                task_id,
                "llm.call",
                payload={
                    "llm_call_id": call.id,
                    "role": role,
                    "purpose": purpose,
                    "model": cfg.model,
                    "attempt": attempt,
                    "ok": ok,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cost_usd": cost,
                    "reserved_usd": reserved_usd,
                    "reservation_id": reservation_id,
                    "latency_ms": latency_ms,
                },
            )
        tracing.call_span(
            "llm.call",
            latency_ms,
            {
                "agentops.task_id": str(task_id),
                "agentops.llm_call_id": call.id,
                "agentops.event_id": event_id,
                "agentops.agent": role,
                "agentops.purpose": purpose,
                "agentops.attempt": attempt,
                "gen_ai.system": cfg.provider,
                "gen_ai.request.model": cfg.model,
                "gen_ai.usage.input_tokens": in_tok,
                "gen_ai.usage.output_tokens": out_tok,
                "agentops.cost_usd": cost,
            },
            error=error,
        )
