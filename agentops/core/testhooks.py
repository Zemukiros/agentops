"""Test-only crash hooks. Inert unless AGENTOPS_TEST_HOOK_DIR is set (never in compose).

When the hook is active for a node, the node writes `<task_id>.<node>.started` (containing the
pid) and blocks until `<task_id>.release` exists, so an integration test can SIGKILL the
worker at a known point inside the node.

- The demo graph's `step_two` blocks whenever the dir is set (Phase 1 behavior).
- Other nodes block only if listed in AGENTOPS_TEST_HOOK_NODES (comma-separated).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

TEST_HOOK_ENV = "AGENTOPS_TEST_HOOK_DIR"
TEST_HOOK_NODES_ENV = "AGENTOPS_TEST_HOOK_NODES"
_HOOK_TIMEOUT_S = 60.0


async def block_if_hooked(task_id: str, node: str, *, always: bool = False) -> None:
    hook_dir = os.environ.get(TEST_HOOK_ENV)
    if not hook_dir:
        return
    nodes = {n.strip() for n in os.environ.get(TEST_HOOK_NODES_ENV, "").split(",") if n.strip()}
    if not always and node not in nodes:
        return
    d = Path(hook_dir)
    (d / f"{task_id}.{node}.started").write_text(str(os.getpid()))
    deadline = time.monotonic() + _HOOK_TIMEOUT_S
    while not (d / f"{task_id}.release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("test hook release file never appeared")
        await asyncio.sleep(0.05)
