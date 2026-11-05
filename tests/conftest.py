"""Test configuration. Forces fake model + fixture search and points the app at the test DB
BEFORE any agentops module reads settings."""

from __future__ import annotations

import os

os.environ["LLM_MODE"] = "fake"
os.environ["SEARCH_MODE"] = "fixture"
os.environ["ENV"] = "test"
_DEFAULT_TEST_DB = "postgresql+psycopg://agentops:agentops@localhost:5433/agentops_test"  # pragma: allowlist secret
os.environ.setdefault("TEST_DATABASE_URL", _DEFAULT_TEST_DB)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["API_TOKEN_PEPPER"] = "test-pepper"  # pragma: allowlist secret
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)
