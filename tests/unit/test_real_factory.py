"""Real-mode configuration checks. Offline: no request is sent."""

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, SecretStr

from agentops.models.anthropic import MarginChatAnthropic, anthropic_factory, real_factory
from agentops.models.client import ModelConfigError
from agentops.settings import LLMMode, RoleModel, Settings

pytestmark = pytest.mark.unit

KEY = SecretStr("sk-test-not-a-real-key")  # pragma: allowlist secret
A = {
    "supervisor": RoleModel(provider="anthropic", model="claude-sonnet-5-5", effort="low"),
    "reviewer": RoleModel(provider="anthropic", model="claude-sonnet-5-5"),
    "writing": RoleModel(provider="anthropic", model="claude-haiku-5-5", max_output_tokens=900),
    "research": RoleModel(provider="anthropic", model="claude-haiku-5-5"),
    "analysis": RoleModel(provider="anthropic", model="claude-sonnet-5-5"),
    "code": RoleModel(provider="anthropic", model="claude-sonnet-5-5"),
}


def test_missing_role_fails_fast() -> None:
    s = Settings(
        llm_mode=LLMMode.REAL, anthropic_api_key=KEY, role_models={"writing": A["writing"]}
    )
    with pytest.raises(ModelConfigError, match="no real model"):
        real_factory(s)


def test_missing_key_fails_fast() -> None:
    s = Settings(_env_file=None, llm_mode=LLMMode.REAL, role_models=A)
    with pytest.raises(ModelConfigError, match="ANTHROPIC_API_KEY"):
        real_factory(s)


def test_unpriced_real_model_fails_fast() -> None:
    roles = {**A, "writing": RoleModel(provider="anthropic", model="claude-unknown-9")}
    s = Settings(llm_mode=LLMMode.REAL, anthropic_api_key=KEY, role_models=roles)
    with pytest.raises(ModelConfigError, match="no verified price"):
        real_factory(s)


class Verdict(BaseModel):
    verdict: str


def test_request_shape_is_safe_for_current_models() -> None:
    """No temperature, no forced tool_choice, explicit max_tokens, effort in output_config;
    structured output goes through output_config.format (json_schema)."""
    s = Settings(llm_mode=LLMMode.REAL, anthropic_api_key=KEY, role_models=A)
    m = real_factory(s)("writing", "draft", A["writing"])
    assert isinstance(m, MarginChatAnthropic) and m.max_retries == 0
    payload = m._get_request_payload([HumanMessage("hi")])
    assert payload["max_tokens"] == 900
    assert "temperature" not in payload and "tool_choice" not in payload

    sup = anthropic_factory(s)("supervisor", "plan", A["supervisor"])
    assert isinstance(sup, MarginChatAnthropic)
    seq = sup.with_structured_output(Verdict, include_raw=True, method="json_schema")
    bound = _find_binding(seq)
    payload = sup._get_request_payload([HumanMessage("hi")], **bound)
    assert payload["output_config"]["effort"] == "low"
    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in payload and "tool_choice" not in payload


def _find_binding(runnable: object) -> dict:
    from langchain_core.runnables import RunnableBinding  # noqa: PLC0415

    seen: set[int] = set()
    stack = [runnable]
    while stack:
        r = stack.pop()
        if id(r) in seen:
            continue
        seen.add(id(r))
        if isinstance(r, RunnableBinding) and "output_config" in r.kwargs:
            return {k: v for k, v in r.kwargs.items() if k != "ls_structured_output_format"}
        for v in getattr(r, "__dict__", {}).values():
            items = v if isinstance(v, list | tuple) else v.values() if isinstance(v, dict) else [v]
            stack.extend(x for x in items if hasattr(x, "invoke"))
    raise AssertionError("structured-output binding not found")
