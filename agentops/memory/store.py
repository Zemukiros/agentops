"""Long-term memory store (ADR-005, ADR-038). Every query filters by user_id.

recall ranks memories with similarity >= MEMORY_MIN_SIMILARITY by
    alpha * similarity + beta * importance + gamma * recency
where recency = 0.5 ** (age_days / MEMORY_HALF_LIFE_DAYS), and keeps MEMORY_TOP_K.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentops.settings import Settings

MEMORY_KINDS = frozenset({"episode", "lesson", "preference", "fact"})


def vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


@dataclass(frozen=True)
class MemoryRow:
    id: uuid.UUID
    kind: str
    text: str
    importance: float
    outcome: str | None
    source_task_id: uuid.UUID | None
    created_at: datetime
    similarity: float | None = None
    score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "text": self.text,
            "importance": self.importance,
            "outcome": self.outcome,
            "source_task_id": str(self.source_task_id) if self.source_task_id else None,
            "created_at": self.created_at.isoformat(),
            "similarity": self.similarity,
            "score": self.score,
        }


_COLS = "id, kind, text, importance, outcome, source_task_id, created_at"


def _row(m: Any, *, sim: float | None = None, score: float | None = None) -> MemoryRow:
    return MemoryRow(
        id=m["id"],
        kind=m["kind"],
        text=m["text"],
        importance=float(m["importance"]),
        outcome=m["outcome"],
        source_task_id=m["source_task_id"],
        created_at=m["created_at"],
        similarity=sim,
        score=score,
    )


async def insert(
    conn: AsyncConnection,
    *,
    user_id: uuid.UUID,
    kind: str,
    text_: str,
    embedding: list[float],
    importance: float,
    outcome: str | None,
    source_task_id: uuid.UUID | None,
    task_type: str | None = None,
) -> uuid.UUID:
    if kind not in MEMORY_KINDS:
        raise ValueError(f"unknown memory kind {kind!r}")
    mid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO memories (id, user_id, kind, text, embedding, importance, outcome, "
            "source_task_id, task_type) VALUES (:id, :u, :k, :t, CAST(:e AS vector), :imp, :o, "
            ":src, :tt)"
        ),
        {
            "id": mid,
            "u": user_id,
            "k": kind,
            "t": text_,
            "e": vec(embedding),
            "imp": max(0.0, min(1.0, importance)),
            "o": outcome,
            "src": source_task_id,
            "tt": task_type,
        },
    )
    return mid


async def find_duplicate(
    conn: AsyncConnection,
    *,
    user_id: uuid.UUID,
    kind: str,
    text_: str,
    embedding: list[float],
    threshold: float,
) -> uuid.UUID | None:
    """An existing live memory of this user and kind with the same text or near-identical
    meaning (cosine >= threshold), if any."""
    row = (
        await conn.execute(
            text(
                "SELECT id FROM memories WHERE user_id = :u AND kind = :k AND NOT superseded "
                "AND (text = :x OR 1 - (embedding <=> CAST(:e AS vector)) >= :thr) "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"u": user_id, "k": kind, "x": text_, "e": vec(embedding), "thr": threshold},
        )
    ).first()
    return row[0] if row else None


async def refresh(
    conn: AsyncConnection,
    memory_id: uuid.UUID,
    *,
    text_: str,
    embedding: list[float],
    importance: float,
    outcome: str | None,
    source_task_id: uuid.UUID | None,
) -> None:
    """A duplicate was seen again: keep ONE memory and make it the latest occurrence (newest
    text, outcome and source task, so the UI links to the run that produced it)."""
    await conn.execute(
        text(
            "UPDATE memories SET created_at = now(), text = :x, embedding = CAST(:e AS vector), "
            "importance = GREATEST(importance, :imp), outcome = COALESCE(:o, outcome), "
            "source_task_id = COALESCE(:src, source_task_id) WHERE id = :id"
        ),
        {
            "id": memory_id,
            "x": text_,
            "e": vec(embedding),
            "imp": max(0.0, min(1.0, importance)),
            "o": outcome,
            "src": source_task_id,
        },
    )


def _parse_vec(raw: Any) -> list[float]:
    if isinstance(raw, str):
        return [float(x) for x in raw.strip("[]").split(",") if x]
    return [float(x) for x in raw]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def dedupe(rows: list[tuple[Any, list[float]]], threshold: float, k: int) -> list[Any]:
    """Greedy: keep rows in rank order, skipping any near-duplicate of one already kept."""
    kept: list[tuple[Any, list[float]]] = []
    for r, v in rows:
        norm = " ".join(str(r.text).lower().split())
        if any(
            norm == " ".join(str(o.text).lower().split()) or _cosine(v, ov) >= threshold
            for o, ov in kept
        ):
            continue
        kept.append((r, v))
        if len(kept) == k:
            break
    return [r for r, _ in kept]


async def recall(
    engine: AsyncEngine,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    query_embedding: list[float],
    min_similarity: float | None = None,
    top_k: int | None = None,
) -> list[MemoryRow]:
    min_sim = settings.memory_min_similarity if min_similarity is None else min_similarity
    k = settings.memory_top_k if top_k is None else top_k
    sql = text(
        f"""
        WITH scored AS (
            SELECT {_COLS}, embedding::text AS emb,
                   1 - (embedding <=> CAST(:q AS vector)) AS sim,
                   power(0.5, extract(epoch FROM now() - created_at) / 86400.0 / :hl) AS recency
            FROM memories
            WHERE user_id = :u AND NOT superseded
        )
        SELECT *, (:a * sim + :b * importance + :g * recency) AS score
        FROM scored WHERE sim >= :min
        ORDER BY score DESC, created_at DESC
        LIMIT :fetch
        """  # noqa: S608  (columns are constants)
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sql,
                {
                    "q": vec(query_embedding),
                    "u": user_id,
                    "hl": settings.memory_half_life_days,
                    "a": settings.memory_alpha,
                    "b": settings.memory_beta,
                    "g": settings.memory_gamma,
                    "min": min_sim,
                    "fetch": k * 4,  # over-fetch, then drop near-duplicates
                },
            )
        ).all()
    unique = dedupe([(r, _parse_vec(r.emb)) for r in rows], settings.memory_dedupe_similarity, k)
    return [_row(r._mapping, sim=float(r.sim), score=float(r.score)) for r in unique]


async def touch(engine: AsyncEngine, ids: list[uuid.UUID]) -> None:
    if not ids:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memories SET access_count = access_count + 1, last_accessed_at = now() "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        )


async def list_for(
    engine: AsyncEngine, user_id: uuid.UUID, *, limit: int, offset: int
) -> list[MemoryRow]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_COLS} FROM memories WHERE user_id = :u "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT :l OFFSET :o"
                ),
                {"u": user_id, "l": limit, "o": offset},
            )
        ).all()
    return [_row(r._mapping) for r in rows]


async def get(engine: AsyncEngine, user_id: uuid.UUID, memory_id: uuid.UUID) -> MemoryRow | None:
    async with engine.connect() as conn:
        r = (
            await conn.execute(
                text(f"SELECT {_COLS} FROM memories WHERE id = :id AND user_id = :u"),  # noqa: S608
                {"id": memory_id, "u": user_id},
            )
        ).first()
    return _row(r._mapping) if r else None


async def delete_one(engine: AsyncEngine, user_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
    async with engine.begin() as conn:
        r = await conn.execute(
            text("DELETE FROM memories WHERE id = :id AND user_id = :u RETURNING id"),
            {"id": memory_id, "u": user_id},
        )
        return r.first() is not None


async def delete_all(engine: AsyncEngine, user_id: uuid.UUID) -> int:
    """All of a user's memories, in one transaction."""
    async with engine.begin() as conn:
        r = await conn.execute(
            text("DELETE FROM memories WHERE user_id = :u RETURNING id"), {"u": user_id}
        )
        return len(r.all())
