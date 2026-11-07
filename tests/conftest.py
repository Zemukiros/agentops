"""Test configuration. Forces fake model + fixture search and points the app at the test DB
BEFORE any agentops module reads settings."""

from __future__ import annotations

import os
import tempfile

os.environ["LLM_MODE"] = "fake"
os.environ["SEARCH_MODE"] = "fixture"
os.environ["ENV"] = "test"
_DEFAULT_TEST_DB = "postgresql+psycopg://agentops:agentops@localhost:5433/agentops_test"  # pragma: allowlist secret
os.environ.setdefault("TEST_DATABASE_URL", _DEFAULT_TEST_DB)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["API_TOKEN_PEPPER"] = "test-pepper"  # pragma: allowlist secret
# Task workspaces and MCP data go to a throwaway dir, never the repo's var/ (used by compose).
_TMP = tempfile.mkdtemp(prefix="agentops-tests-")
os.environ["WORKSPACE_ROOT"] = os.path.join(_TMP, "workspaces")
os.environ.pop("WORKSPACE_HOST_ROOT", None)
os.environ["MCP_DATA_DIR"] = os.path.join(_TMP, "mcp")
os.environ["MCP_SERVERS"] = ""
# Deterministic bag-of-words embeddings; the real fastembed model is exercised by e2e/model tests.
os.environ["EMBEDDING_MODE"] = "hash"
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY"):
    os.environ.pop(_k, None)
