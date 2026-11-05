"""Per-task budget ledger (ADR-024): reserve the worst case before every model attempt,
settle with actual usage afterwards.

Why a DB ledger instead of a counter in graph state: parallel branches (Phase 3) each get a
copy of graph state, so a state counter can't stop two branches from spending the same
remaining budget. One atomic `UPDATE … WHERE used + reserved + worst_case <= max` can.

Guarantee: a model call starts only after its worst-case cost (exact input tokens +
`max_output_tokens`, priced from the versioned price table) has been reserved, so no single
call can take a task over its limit.

Crash between reserve and settle: the reservation stays `reserved`. The next run segment
(which holds the thread's advisory lock, so nothing else is spending for this task) charges
it at its worst-case amount and marks it `orphaned`. The provider may have billed that call,
so over-counting is the honest direction.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from agentops.settings import Budget

log = logging.getLogger(__name__)


class BudgetState(BaseModel):
    """Read-only snapshot of a task's ledger row."""

    model_config = ConfigDict(frozen=True)

    max_tokens: int
    max_usd: float
    max_llm_calls: int
    tokens_used: int = 0
    usd_used: float = 0.0
    llm_calls: int = 0
    unpriced_calls: int = 0  # calls whose cost is unknown (unverified price); see pricing.py
    tokens_reserved: int = 0
    usd_reserved: float = 0.0
    calls_reserved: int = 0

    @classmethod
    def from_limits(cls, limits: Budget) -> BudgetState:
        return cls(
            max_tokens=limits.max_tokens,
            max_usd=limits.max_usd,
            max_llm_calls=limits.max_llm_calls,
        )

    def refusal_reason(self, *, usd: float, tokens: int, calls: int = 1) -> str | None:
        """Why a reservation of this size would be refused, or None if it fits."""
        if self.llm_calls + self.calls_reserved + calls > self.max_llm_calls:
            return f"llm call limit reached ({self.llm_calls}/{self.max_llm_calls})"
        if self.tokens_used + self.tokens_reserved + tokens > self.max_tokens:
            return (
                f"token limit: next call may use {tokens}, "
                f"{self.max_tokens - self.tokens_used - self.tokens_reserved} left "
                f"(used {self.tokens_used}/{self.max_tokens})"
            )
        if self.usd_used + self.usd_reserved + usd > self.max_usd + 1e-9:
            return (
                f"cost limit: next call may cost ${usd:.6f}, "
                f"${self.max_usd - self.usd_used - self.usd_reserved:.6f} left "
                f"(used ${self.usd_used:.6f}/${self.max_usd:.2f})"
            )
        return None

    @property
    def fraction_used(self) -> float:
        return max(
            self.tokens_used / self.max_tokens,
            self.usd_used / self.max_usd if self.max_usd else 0.0,
            self.llm_calls / self.max_llm_calls,
        )


class BudgetExceededError(Exception):
    """Raised before a model call when its worst case doesn't fit in the remaining budget."""

    def __init__(self, reason: str, state: BudgetState | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state


class BudgetNotInitializedError(RuntimeError):
    """The task has no ledger row (created with the task by POST /tasks)."""


USD_SCALE = 10  # NUMERIC(18, 10) in task_budgets / budget_reservations


def usd_round_up(usd: float) -> float:
    """Round a worst-case amount UP to the ledger's scale, so the stored reservation is never
    smaller than the computed worst case and reserve/settle use the identical value."""
    q = 10**USD_SCALE
    return float(math.ceil(usd * q - 1e-6) / q)


@dataclass(frozen=True)
class Reservation:
    id: int
    task_id: uuid.UUID
    usd: float
    tokens: int
    calls: int


_SNAPSHOT_SQL = text(
    "SELECT max_tokens, max_usd, max_calls, used_tokens, used_usd, used_calls, unpriced_calls, "
    "reserved_tokens, reserved_usd, reserved_calls FROM task_budgets WHERE task_id = :t"
)

# One statement: the conditional UPDATE is the admission check, and the reservation row is
# only inserted when it succeeded. Concurrent reservers serialize on the row lock.
_RESERVE_SQL = text(
    """
    WITH granted AS (
        UPDATE task_budgets SET
            reserved_usd = reserved_usd + CAST(:usd AS numeric),
            reserved_tokens = reserved_tokens + :tokens,
            reserved_calls = reserved_calls + :calls,
            updated_at = now()
        WHERE task_id = :t
          AND used_usd + reserved_usd + CAST(:usd AS numeric) <= max_usd
          AND used_tokens + reserved_tokens + :tokens <= max_tokens
          AND used_calls + reserved_calls + :calls <= max_calls
        RETURNING task_id
    )
    INSERT INTO budget_reservations (task_id, usd, tokens, calls)
    SELECT task_id, CAST(:usd AS numeric), :tokens, :calls FROM granted
    RETURNING id
    """
)


def _state(row: object) -> BudgetState:
    m = row._mapping  # type: ignore[attr-defined]
    return BudgetState(
        max_tokens=m["max_tokens"],
        max_usd=float(m["max_usd"]),
        max_llm_calls=m["max_calls"],
        tokens_used=m["used_tokens"],
        usd_used=float(m["used_usd"]),
        llm_calls=m["used_calls"],
        unpriced_calls=m["unpriced_calls"],
        tokens_reserved=m["reserved_tokens"],
        usd_reserved=float(m["reserved_usd"]),
        calls_reserved=m["reserved_calls"],
    )


async def open_budget_on(conn: AsyncConnection, task_id: uuid.UUID, limits: Budget) -> None:
    """Create the task's ledger row (idempotent). Called in the task-creation transaction."""
    await conn.execute(
        text(
            "INSERT INTO task_budgets (task_id, max_usd, max_tokens, max_calls) "
            "VALUES (:t, :u, :k, :c) ON CONFLICT (task_id) DO NOTHING"
        ),
        {"t": task_id, "u": limits.max_usd, "k": limits.max_tokens, "c": limits.max_llm_calls},
    )


class BudgetLedger:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def snapshot(self, task_id: uuid.UUID) -> BudgetState:
        async with self._sm() as s:
            row = (await s.execute(_SNAPSHOT_SQL, {"t": task_id})).first()
        if row is None:
            raise BudgetNotInitializedError(f"no budget for task {task_id}")
        return _state(row)

    async def reserve(
        self, task_id: uuid.UUID, *, usd: float, tokens: int, calls: int = 1
    ) -> Reservation:
        if usd < 0 or tokens < 0 or calls < 0:
            raise ValueError("reservation amounts must be non-negative")
        usd = usd_round_up(usd)
        async with self._sm() as s, s.begin():
            rid = (
                await s.execute(
                    _RESERVE_SQL, {"t": task_id, "usd": usd, "tokens": tokens, "calls": calls}
                )
            ).scalar_one_or_none()
        if rid is not None:
            return Reservation(int(rid), task_id, usd, tokens, calls)
        state = await self.snapshot(task_id)  # raises if the row is missing
        reason = state.refusal_reason(usd=usd, tokens=tokens, calls=calls) or "budget exhausted"
        raise BudgetExceededError(reason, state)

    async def settle(self, res: Reservation, *, usd: float | None, tokens: int) -> None:
        """Replace the reservation with actual usage. `usd=None` means the price is unknown
        (recorded as an unpriced call, never guessed)."""
        actual_usd = usd or 0.0
        if tokens > res.tokens or actual_usd > res.usd + 1e-9:
            # Estimation bug: the reservation did not cover the call. Record the truth anyway.
            log.error(
                "call exceeded its reservation %s: tokens %s > %s or usd %s > %s",
                res.id,
                tokens,
                res.tokens,
                actual_usd,
                res.usd,
            )
        async with self._sm() as s, s.begin():
            held = (
                await s.execute(
                    text(
                        "UPDATE budget_reservations SET status = 'settled', settled_at = now() "
                        "WHERE id = :id AND status = 'reserved' RETURNING id"
                    ),
                    {"id": res.id},
                )
            ).first()
            # If the reservation was already charged as an orphan, its worst case is in
            # `used` already; don't count the call twice.
            if held is None:
                log.warning("reservation %s was no longer held at settle time", res.id)
                return
            await s.execute(
                text(
                    """
                    UPDATE task_budgets SET
                        reserved_usd = reserved_usd - CAST(:rusd AS numeric),
                        reserved_tokens = reserved_tokens - :rtok,
                        reserved_calls = reserved_calls - :rcalls,
                        used_usd = used_usd + CAST(:usd AS numeric),
                        used_tokens = used_tokens + :tokens,
                        used_calls = used_calls + :rcalls,
                        unpriced_calls = unpriced_calls + :unpriced,
                        updated_at = now()
                    WHERE task_id = :t
                    """
                ),
                {
                    "t": res.task_id,
                    "rusd": res.usd,
                    "rtok": res.tokens,
                    "rcalls": res.calls,
                    "usd": actual_usd,
                    "tokens": tokens,
                    "unpriced": 1 if usd is None else 0,
                },
            )

    async def charge_orphans(self, task_id: uuid.UUID) -> int:
        """Charge reservations left open by a crashed segment at their worst case.
        Call only while holding the thread's advisory lock. Returns how many were charged."""
        async with self._sm() as s, s.begin():
            rows = (
                await s.execute(
                    text(
                        "UPDATE budget_reservations SET status = 'orphaned', settled_at = now() "
                        "WHERE task_id = :t AND status = 'reserved' "
                        "RETURNING usd, tokens, calls"
                    ),
                    {"t": task_id},
                )
            ).all()
            if not rows:
                return 0
            usd = sum(float(r[0]) for r in rows)
            tokens = sum(int(r[1]) for r in rows)
            calls = sum(int(r[2]) for r in rows)
            await s.execute(
                text(
                    """
                    UPDATE task_budgets SET
                        reserved_usd = reserved_usd - CAST(:usd AS numeric),
                        reserved_tokens = reserved_tokens - :tokens,
                        reserved_calls = reserved_calls - :calls,
                        used_usd = used_usd + CAST(:usd AS numeric),
                        used_tokens = used_tokens + :tokens,
                        used_calls = used_calls + :calls,
                        updated_at = now()
                    WHERE task_id = :t
                    """
                ),
                {"t": task_id, "usd": usd, "tokens": tokens, "calls": calls},
            )
        log.warning(
            "charged %d orphaned reservation(s) for task %s at worst case", len(rows), task_id
        )
        return len(rows)
