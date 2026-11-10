"""Recorded cassettes: the first real run of a cassette scenario is saved (every successful
model call, grouped by role + purpose in call order, plus the checks and the judge verdict).
Replay serves those responses through the fake model, so CI re-runs the real orchestration,
tools and trajectory checks for free and compares them with the recorded outcome."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.models.fake import FakeScript, ScriptedTurn

CASSETTE_DIR = Path(__file__).parent / "cassettes"
STRUCTURED = ("plan", "review", "final_review", "consolidate")


def is_structured(purpose: str) -> bool:
    return purpose.split("/", 1)[0] in STRUCTURED


def to_turn(purpose: str, response: dict[str, Any]) -> ScriptedTurn:
    content = str(response.get("content") or "")
    if is_structured(purpose):
        return ScriptedTurn(structured=json.loads(content))
    calls = [{"name": c["name"], "args": c["args"]} for c in response.get("tool_calls") or []]
    return ScriptedTurn(content=content, tool_calls=calls)


async def record(
    engine: AsyncEngine, scenario: Any, task_id: uuid.UUID, outcome: dict[str, Any]
) -> Path:
    async with engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT role, purpose, response FROM llm_calls WHERE task_id=:t AND ok "
                    "ORDER BY id"
                ),
                {"t": task_id},
            )
        ).all()
    calls = [{"role": r.role, "purpose": r.purpose, "response": r.response} for r in rows]
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    path = CASSETTE_DIR / f"{scenario.id}.json"
    path.write_text(
        json.dumps(
            {
                "scenario": scenario.id,
                "fingerprint": scenario.fingerprint(),
                "calls": calls,
                "expected": {
                    "checks": {c["name"]: c["pass"] for c in outcome.get("checks", [])},
                    "judge": {
                        k: v for k, v in (outcome.get("judge") or {}).items() if k != "prompt"
                    },
                    "passed": outcome.get("passed"),
                },
            },
            indent=1,
            default=str,
        )
        + "\n"
    )
    return path


def script_for(data: dict[str, Any]) -> FakeScript:
    s = FakeScript()
    scen = f"cassette:{data['scenario']}"
    grouped: dict[tuple[str, str], list[ScriptedTurn]] = {}
    for call in data["calls"]:
        grouped.setdefault((call["role"], call["purpose"]), []).append(
            to_turn(call["purpose"], call["response"] or {})
        )
    for (role, purpose), turns in grouped.items():
        s.add(role, scen, *turns, purpose=purpose)
    return s


def load_all(directory: Path = CASSETTE_DIR) -> dict[str, FakeScript]:
    return {
        (d := json.loads(f.read_text()))["scenario"]: script_for(d)
        for f in sorted(directory.glob("*.json"))
    }


def expected(directory: Path = CASSETTE_DIR) -> dict[str, dict[str, Any]]:
    return {
        (d := json.loads(f.read_text()))["scenario"]: d for f in sorted(directory.glob("*.json"))
    }


def merged(scripts: dict[str, FakeScript]) -> FakeScript:
    out = FakeScript()
    for s in scripts.values():
        for key, turns in s.turns.items():
            out.turns.setdefault(key, []).extend(turns)
    return out
