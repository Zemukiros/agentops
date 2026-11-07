"""Memory service: recall for planning, consolidation at the end of a run (ADR-038).

What can become a memory:
- an EPISODE for every finished run: the request and its outcome (always written, no LLM)
- lessons / preferences / facts proposed by one supervisor call from REVIEWER-ACCEPTED
  outputs, the final outcome and human decisions. Raw tool output is not in that input.
Every candidate is then screened and dropped if it
- shares any 8-word sequence with a tool output of this task (copied web/file/SQL/program text,
  e.g. a prompt-injection page an accepted output quoted), or
- looks like an instruction to an agent (ignore previous instructions, call a tool, ...).
Each memory carries source_task_id and the run's outcome, so lessons from failed runs say so.

If the consolidation call can't run (the ledger refuses it, or the model fails), only the
episode is written: consolidation never fails a run or overshoots its budget (Phase 4
change 2). Worker-level terminals (budget_exceeded, recursion) also write their episode.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.memory import store
from agentops.memory.embedder import Embedder, make_embedder
from agentops.settings import Settings

SHINGLE = 8
_WORDS = re.compile(r"[a-z0-9]+")
_FRAME = re.compile(r"</?tool_result[^>]*>")
_INSTRUCTION = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) instructions|system instruction|"
    r"you are now|do not (tell|mention)|maintenance mode|\bcall (the )?\w+(_\w+)+\b|"
    r"\b(send_email|delete_note|run_python|fetch_url)\b|attacker@)"
)


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["lesson", "preference", "fact"]
    text: str = Field(min_length=10, max_length=400)
    importance: float = Field(default=0.5, ge=0, le=1)


class MemoryCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=5)


def shingles(text_: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    words = _WORDS.findall(_FRAME.sub(" ", text_.lower()))
    return {tuple(words[i : i + n]) for i in range(max(0, len(words) - n + 1))}


def screen(
    candidates: list[MemoryCandidate], tool_shingles: set[tuple[str, ...]]
) -> tuple[list[MemoryCandidate], list[tuple[str, str]]]:
    kept: list[MemoryCandidate] = []
    dropped: list[tuple[str, str]] = []
    for c in candidates:
        if _INSTRUCTION.search(c.text):
            dropped.append((c.text[:120], "looks like an instruction"))
        elif shingles(c.text) & tool_shingles:
            dropped.append((c.text[:120], "copies raw tool output"))
        else:
            kept.append(c)
    return kept, dropped


@dataclass(frozen=True)
class Written:
    ids: list[uuid.UUID]
    dropped: list[tuple[str, str]]


class MemoryService:
    def __init__(self, settings: Settings, engine: AsyncEngine, embedder: Embedder | None = None):
        self.settings = settings
        self.engine = engine
        self.embedder = embedder or make_embedder(settings)

    async def recall(self, user_id: uuid.UUID, query: str) -> list[store.MemoryRow]:
        [q] = await self.embedder.embed([query])
        return await store.recall(self.engine, self.settings, user_id=user_id, query_embedding=q)

    async def tool_shingles(self, task_id: uuid.UUID) -> set[tuple[str, ...]]:
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT coalesce(output_text, output_preview, '') FROM tool_calls "
                            "WHERE task_id = :t"
                        ),
                        {"t": task_id},
                    )
                )
                .scalars()
                .all()
            )
        out: set[tuple[str, ...]] = set()
        for r in rows:
            out |= shingles(r)
        return out

    async def consolidate(
        self,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        request: str,
        status: str,
        reason: str | None,
        candidates: list[MemoryCandidate],
    ) -> Written:
        kept, dropped = screen(candidates, await self.tool_shingles(task_id))
        episode = episode_text(request, status, reason)
        texts = [episode, *[c.text for c in kept]]
        vectors = await self.embedder.embed(texts)
        ids: list[uuid.UUID] = []
        async with self.engine.begin() as conn:
            items = [("episode", episode, 0.3), *[(c.kind, c.text, c.importance) for c in kept]]
            for (kind, text_, importance), v in zip(items, vectors, strict=True):
                # Idempotent: a re-run of consolidate can't write the same memory twice.
                exists = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM memories WHERE source_task_id = :t AND kind = :k "
                            "AND text = :x"
                        ),
                        {"t": task_id, "k": kind, "x": text_},
                    )
                ).first()
                if exists:
                    continue
                ids.append(
                    await store.insert(
                        conn,
                        user_id=user_id,
                        kind=kind,
                        text_=text_,
                        embedding=v,
                        importance=importance,
                        outcome=status,
                        source_task_id=task_id,
                    )
                )
        return Written(ids=ids, dropped=dropped)

    async def write_episode(
        self,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        request: str,
        status: str,
        reason: str | None,
    ) -> Written:
        return await self.consolidate(
            task_id=task_id,
            user_id=user_id,
            request=request,
            status=status,
            reason=reason,
            candidates=[],
        )


def episode_text(request: str, status: str, reason: str | None) -> str:
    req = " ".join(request.split())[:300]
    tail = f": {' '.join(reason.split())[:300]}" if reason else ""
    return f'Past task "{req}" ended {status}{tail}'
