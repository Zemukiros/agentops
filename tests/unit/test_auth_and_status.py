import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from agentops.api.auth import CurrentUser, hash_token, owned_by
from agentops.db.models import TERMINAL_TASK_STATUSES, Task, TaskStatus, UserRole
from agentops.queue.jobs import MAX_BACKOFF_SECONDS, backoff_seconds

pytestmark = pytest.mark.unit


def test_token_hash_is_keyed_and_stable() -> None:
    assert hash_token("abc", "p1") == hash_token("abc", "p1")
    assert hash_token("abc", "p1") != hash_token("abc", "p2")
    assert len(hash_token("abc", "p1")) == 64
    assert "abc" not in hash_token("abc", "p1")


def test_owned_by_adds_owner_filter() -> None:
    user = CurrentUser(id=uuid.uuid4(), name="a", role=UserRole.USER)
    stmt = owned_by(select(Task), Task.owner_id, user)
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "tasks.owner_id = " in sql


def test_terminal_statuses_match_decisions_md() -> None:
    assert {s.value for s in TERMINAL_TASK_STATUSES} == {
        "completed",
        "completed_partial",
        "failed",
        "rejected",
        "cancelled",
        "budget_exceeded",
    }
    non_terminal = set(TaskStatus) - TERMINAL_TASK_STATUSES
    assert {s.value for s in non_terminal} == {"queued", "running", "awaiting_approval"}


def test_backoff_is_capped() -> None:
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(30) == MAX_BACKOFF_SECONDS
