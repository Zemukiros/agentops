"""ADR-028: every Pydantic class that can end up in a checkpoint is in the serde allowlist."""

import typing

import pytest
from pydantic import BaseModel

from agentops.core import state as st

pytestmark = pytest.mark.unit


def _models_in(tp: object, seen: set[type]) -> set[type[BaseModel]]:
    found: set[type[BaseModel]] = set()
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        # LangChain messages are on LangGraph's built-in safe list (verified round-trip).
        if tp.__module__.startswith("langchain_core.") or tp in seen:
            return found
        seen.add(tp)
        found.add(tp)
        for f in tp.model_fields.values():
            found |= _models_in(f.annotation, seen)
        return found
    for arg in typing.get_args(tp):
        found |= _models_in(arg, seen)
    return found


def test_all_state_models_are_registered() -> None:
    seen: set[type] = set()
    reachable: set[type[BaseModel]] = set()
    for td in (st.TaskState, st.SubtaskState):
        for hint in typing.get_type_hints(td, include_extras=True).values():
            reachable |= _models_in(hint, seen)
    assert reachable, "found no models; the walker is broken"
    missing = {c.__name__ for c in reachable} - {c.__name__ for c in st.CHECKPOINT_TYPES}
    assert not missing, f"add to CHECKPOINT_TYPES: {sorted(missing)}"
