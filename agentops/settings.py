"""Application settings. All configuration comes from the environment (or `.env`).

Secrets are `SecretStr` so they never print in reprs, logs, or tracebacks.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Role = Literal["supervisor", "research", "analysis", "writing", "code", "reviewer"]
ROLES: tuple[Role, ...] = ("supervisor", "research", "analysis", "writing", "code", "reviewer")


_DEV_PEPPER = "dev-only-pepper-change-me"  # pragma: allowlist secret
_DEV_SQL_PASSWORD = "dev-only-readonly-change-me"  # noqa: S105  # pragma: allowlist secret


class LLMMode(StrEnum):
    FAKE = "fake"
    REAL = "real"


class SearchMode(StrEnum):
    FIXTURE = "fixture"
    LIVE = "live"


class RoleModel(BaseModel):
    """Which model serves a role. `provider` is 'fake' in fake mode.

    No sampling parameters: current Anthropic models reject non-default temperature/top_p
    (HTTP 400), so none are sent (verified 2026-11-05, ADR-009).
    """

    provider: str = "fake"
    model: str = "fake-scripted-v1"
    # Hard cap on output tokens per call, thinking included. The budget ledger reserves it.
    max_output_tokens: int = 2048
    # Anthropic `output_config.effort` (low|medium|high|xhigh|max). None = model default.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


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
    # SEARCH_MODE=live only (free tier needs an account); tests never use it.
    tavily_api_key: SecretStr | None = None
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
    llm_timeout_seconds: float = 120.0
    # A partial deliverable (some subtasks failed/blocked) needs at least this reviewer score
    # (1-5) to end completed_partial; below it the run ends failed (ADR-029).
    final_review_min_score: int = Field(default=3, ge=1, le=5)
    # plan_gate (ADR-036): effective plan confidence below this -> human approves the plan.
    plan_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    # Approvals (ADR-035): a blocking approval left pending this long resolves as reject.
    approval_ttl_seconds: int = Field(default=86_400, ge=1)
    # Highest per-task max_usd a reviewer may approve when a run hits its budget.
    budget_max_raise_usd: float = Field(default=2.0, gt=0)
    # Real-mode input estimate margin (ADR-024): count_tokens can't see the structured-output
    # schema (sent as output_config.format) or the thinking config, so the reserved input is
    # ceil(counted * ratio) + tokens. Each llm.call event records estimate vs actual.
    llm_input_margin_ratio: float = 1.15
    llm_input_margin_tokens: int = 512

    # Per-task workspaces (ADR-008/011). WORKSPACE_ROOT is the path as this process sees it;
    # WORKSPACE_HOST_ROOT is the same directory as the Docker host sees it (the sandbox's bind
    # mount source). They differ when the worker runs in a container.
    workspace_root: Path = Path("var/workspaces")
    workspace_host_root: Path | None = None

    # SQL tools (ADR-033): a separate LOGIN role with SELECT on the `demo` schema only, read-only
    # transactions and a statement timeout. Same host/database as DATABASE_URL.
    sql_tool_role: str = "agentops_readonly"
    sql_tool_password: SecretStr = SecretStr(_DEV_SQL_PASSWORD)
    sql_tool_statement_timeout_ms: int = 3000

    # MCP (ADR-032): config/mcp_servers.toml is the allowlist; MCP_SERVERS picks which of its
    # servers this worker starts (comma-separated, e.g. "notes,notes_http"). Empty = none.
    mcp_config_path: Path = Path("config/mcp_servers.toml")
    mcp_servers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    mcp_data_dir: Path = Path("var/mcp")

    # Mail (Mailpit in dev: captures, sends nothing out)
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "agentops@localhost"
    # Mailpit HTTP API as this process sees it (lookup), and its UI as the browser sees it (links)
    mailpit_api_url: str = "http://localhost:8025"
    mailpit_ui_url: str = "http://localhost:8025"

    # Web UI (ADR-042): the only origins allowed by CORS (comma-separated).
    ui_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    # Upper bounds for a budget a user sets on a new task (POST /tasks).
    task_budget_max_usd: float = Field(default=2.0, gt=0)
    task_budget_max_tokens: int = Field(default=2_000_000, gt=0)
    task_budget_max_llm_calls: int = Field(default=500, gt=0)

    # Embeddings (ADR-010: fastembed BAAI/bge-small-en-v1.5 = 384 dims; must match migration)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # fastembed (real) or hash (deterministic bag-of-words; tests and offline dev)
    embedding_mode: Literal["fastembed", "hash"] = "fastembed"
    # FASTEMBED_CACHE_PATH is also the variable fastembed itself reads; the image sets it.
    fastembed_cache_path: Path = Path("var/fastembed")
    # Long-term memory (ADR-038): recall keeps memories with similarity >= the minimum, ranks
    # them by alpha*sim + beta*importance + gamma*recency (half-life in days), keeps top_k.
    memory_min_similarity: float = Field(default=0.6, ge=-1, le=1)
    memory_top_k: int = Field(default=5, ge=1, le=20)
    memory_alpha: float = 0.7
    memory_beta: float = 0.2
    memory_gamma: float = 0.1
    memory_half_life_days: float = Field(default=30.0, gt=0)

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"

    @field_validator("mcp_servers", "ui_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

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
        if self.env == "prod" and self.sql_tool_password.get_secret_value() == _DEV_SQL_PASSWORD:
            raise ValueError("SQL_TOOL_PASSWORD must be set in prod")
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

    @property
    def sql_tool_dsn(self) -> str:
        """libpq DSN for the read-only SQL-tool role (same host and database)."""
        from sqlalchemy.engine import make_url  # noqa: PLC0415

        url = make_url(self.libpq_dsn).set(
            username=self.sql_tool_role, password=self.sql_tool_password.get_secret_value()
        )
        return url.render_as_string(hide_password=False)


EMBEDDING_DIM = 384  # schema constant; migration 0001 uses it. Settings must agree.


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
