"""Pure helpers behind the Phase 5 read APIs: redaction, time breakdown, task budgets."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from agentops.api.routes.tasks import BudgetIn, task_budget
from agentops.api.views import time_breakdown
from agentops.observability.redact import REDACTED, redact
from agentops.settings import Settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnop",
        "sk-" + "a" * 40,
        "tvly-abcdefghijkl",
        "ghp_" + "b" * 36,
        "AKIAABCDEFGHIJKLMNOP",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "postgresql://agentops:hunter2@db:5432/x",
    ],
)
def test_secret_shaped_strings_are_masked_anywhere(secret: str) -> None:
    out = redact({"note": f"before {secret} after", "list": [secret]})
    assert secret not in str(out)
    assert REDACTED in out["note"] and out["note"].startswith("before ")


@pytest.mark.parametrize(
    "key", ["api_key", "API-KEY", "Authorization", "password", "client_secret", "x-token", "dsn"]
)
def test_secret_named_keys_are_replaced(key: str) -> None:
    assert redact({key: "anything at all"})[key] == REDACTED


def test_counters_and_ids_with_secretish_names_survive() -> None:
    d = {"input_tokens": 12, "max_tokens": 50, "tool_call_id": "abc", "llm_call_id": 3}
    assert redact(d) == d


def test_truncation_and_nesting() -> None:
    out = redact({"a": {"b": ["x" * 50]}}, max_chars=10)
    assert out["a"]["b"][0].startswith("x" * 10) and "truncated 40 chars" in out["a"]["b"][0]
    assert len(redact(list(range(500)))) == 201


T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def ev(t: str, s: float, status: str | None = None) -> dict:
    return {
        "type": t,
        "created_at": T0 + timedelta(seconds=s),
        "payload": {"status": status} if status else {},
    }


def test_time_compute_wait_queue_with_a_pause_and_a_crash() -> None:
    events = [
        ev("run.segment_started", 2),  # queued 2 s
        ev("task.status", 5, "awaiting_approval"),  # compute 3 s
        ev("task.status", 65, "running"),  # human 60 s
        ev("run.segment_started", 66),  # queued 1 s
        ev("run.segment_started", 70),  # crash: 4 s compute, then a new segment
        ev("task.finished", 72),  # compute 2 s
    ]
    t = time_breakdown(events, T0, T0 + timedelta(hours=1), terminal=True)
    assert t == {"wall_s": 72.0, "compute_s": 9.0, "human_wait_s": 60.0, "queue_s": 3.0}


def test_time_while_still_waiting_counts_to_now() -> None:
    events = [ev("run.segment_started", 1), ev("task.status", 3, "awaiting_approval")]
    t = time_breakdown(events, T0, T0 + timedelta(seconds=10), terminal=False)
    assert (t["compute_s"], t["human_wait_s"], t["wall_s"]) == (2.0, 7.0, 10.0)


def test_task_budget_defaults_overrides_and_caps() -> None:
    s = Settings()
    assert task_budget(s, None) == s.budget
    b = task_budget(s, BudgetIn(max_usd=0.3))
    assert b.max_usd == 0.3 and b.max_llm_calls == s.budget.max_llm_calls
    with pytest.raises(HTTPException) as e:
        task_budget(s, BudgetIn(max_llm_calls=s.task_budget_max_llm_calls + 1))
    assert e.value.status_code == 422
