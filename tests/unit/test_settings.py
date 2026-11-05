import pytest
from pydantic import SecretStr, ValidationError

from agentops.settings import EMBEDDING_DIM, LLMMode, SearchMode, Settings

pytestmark = pytest.mark.unit


def test_tests_run_in_fake_and_fixture_mode() -> None:
    s = Settings()
    assert s.llm_mode is LLMMode.FAKE
    assert s.search_mode is SearchMode.FIXTURE


def test_secrets_never_render() -> None:
    s = Settings(anthropic_api_key=SecretStr("sk-very-secret"))  # pragma: allowlist secret
    assert "sk-very-secret" not in repr(s)
    assert "sk-very-secret" not in str(s.model_dump())


def test_libpq_dsn_strips_driver() -> None:
    s = Settings(database_url=SecretStr("postgresql+psycopg://u:p@h:1/db"))
    assert s.libpq_dsn == "postgresql://u:p@h:1/db"


def test_per_role_models_and_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROLE_MODELS__REVIEWER__PROVIDER", "anthropic")
    monkeypatch.setenv("ROLE_MODELS__REVIEWER__MODEL", "some-model")
    monkeypatch.setenv("BUDGET__MAX_USD", "0.25")
    s = Settings()
    assert s.model_for("reviewer").provider == "anthropic"
    assert s.model_for("reviewer").model == "some-model"
    assert s.budget.max_usd == 0.25


def test_embedding_dim_must_match_schema() -> None:
    with pytest.raises(ValidationError):
        Settings(embedding_dim=EMBEDDING_DIM + 1)


def test_prod_requires_pepper() -> None:
    with pytest.raises(ValidationError):
        Settings(env="prod", api_token_pepper=SecretStr(""))
