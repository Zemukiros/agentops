"""Application settings. All configuration comes from the environment (or `.env`).

Secrets are `SecretStr` so they never print in reprs, logs, or tracebacks.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["supervisor", "research", "analysis", "writing", "code", "reviewer"]
ROLES: tuple[Role, ...] = ("supervisor", "research", "analysis", "writing", "code", "reviewer")


_DEV_PEPPER = "dev-only-pepper-change-me"  # pragma: allowlist secret


class LLMMode(StrEnum):
    FAKE = "fake"
    REAL = "real"


class SearchMode(StrEnum):
    FIXTURE = "fixture"
    LIVE = "live"


class RoleModel(BaseModel):
    """Which model serves a role. `provider` is 'fake' in fake mode."""

    provider: str = "fake"
    model: str = "fake-scripted-v1"
    temperature: float = 0.0
    max_output_tokens: int = 2048


class Budget(BaseModel):
    max_tokens: int = 200_000
    max_usd: float = 1.00
    max_llm_calls: int = 60
    max_steps_per_specialist: int = 8
    recursion_limit: int = 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    llm_mode: LLMMode = LLMMode.FAKE
    search_mode: SearchMode = SearchMode.FIXTURE

    # Per-role models: override e.g. ROLE_MODELS__REVIEWER__MODEL=...
    role_models: dict[str, RoleModel] = Field(
        default_factory=lambda: {str(r): RoleModel() for r in ROLES}
    )
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    budget: Budget = Field(default_factory=Budget)

    # SQLAlchemy URL (psycopg3 async driver). The checkpointer derives a libpq DSN from it.
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://agentops:agentops@localhost:5433/agentops"  # pragma: allowlist secret
    )
    db_pool_size: int = 10

    # Auth: API tokens are stored as HMAC-SHA256(pepper, token).
    api_token_pepper: SecretStr = SecretStr(_DEV_PEPPER)

    # Job queue
    job_lease_seconds: float = 30.0
    job_heartbeat_seconds: float = 10.0
    job_poll_seconds: float = 0.5
    job_max_attempts: int = 5

    # Model client retries
    llm_max_retries: int = 2
    llm_retry_base_seconds: float = 0.5

    # Mail (Mailpit in dev: captures, sends nothing out)
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "agentops@localhost"

    # Embeddings (ADR-010: fastembed BAAI/bge-small-en-v1.5 = 384 dims; must match migration)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"

    @field_validator("api_token_pepper", mode="before")
    @classmethod
    def _empty_pepper_is_dev(cls, v: object) -> object:
        if v is None or v == "" or (isinstance(v, SecretStr) and not v.get_secret_value()):
            return _DEV_PEPPER
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.embedding_dim != EMBEDDING_DIM:
            raise ValueError(
                f"embedding_dim={self.embedding_dim} but the schema uses {EMBEDDING_DIM}; "
                "changing it requires a migration"
            )
        if self.env == "prod" and self.api_token_pepper.get_secret_value() == _DEV_PEPPER:
            raise ValueError("API_TOKEN_PEPPER must be set in prod")
        return self

    def model_for(self, role: str) -> RoleModel:
        return self.role_models.get(role, RoleModel())

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def libpq_dsn(self) -> str:
        """Plain `postgresql://` DSN for psycopg / the LangGraph checkpointer."""
        url = self.sqlalchemy_url
        for prefix in ("postgresql+psycopg://", "postgresql+psycopg_async://"):
            if url.startswith(prefix):
                return "postgresql://" + url[len(prefix) :]
        return url


EMBEDDING_DIM = 384  # schema constant; migration 0001 uses it. Settings must agree.


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
