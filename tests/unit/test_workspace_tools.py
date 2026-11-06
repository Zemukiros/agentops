"""Workspace tools: traversal, absolute paths, symlinks, caps (ADR-008)."""

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from agentops.settings import Settings
from agentops.tools.native import workspace as ws
from agentops.tools.spec import ToolContext

pytestmark = pytest.mark.unit


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    settings = Settings(workspace_root=tmp_path / "root")
    tid = uuid.uuid4()
    return ToolContext(
        task_id=tid,
        owner_id=None,
        agent="writing",
        subtask_id="s",
        revision=0,
        step=1,
        workspace=ws.task_workspace(settings, tid),
        settings=settings,
        engine=None,  # type: ignore[arg-type]  # workspace tools never touch the DB
    )


async def call(fn: Any, model: Any, ctx: ToolContext, **kw: Any) -> str:
    return (await fn(model(**kw), ctx)).text


async def test_write_read_list_roundtrip(ctx: ToolContext) -> None:
    await call(ws.workspace_write, ws.WriteIn, ctx, path="notes/a.md", content="héllo")
    assert await call(ws.workspace_read, ws.ReadIn, ctx, path="notes/a.md") == "héllo"
    listing = await call(ws.workspace_list, ws.ListIn, ctx, path="notes")
    assert listing == "file\t6\tnotes/a.md"
    assert oct(ctx.workspace.stat().st_mode & 0o777) == "0o777"


@pytest.mark.parametrize(
    "bad", ["../escape.txt", "a/../../escape.txt", "/etc/passwd", "~/x", "", "a\x00b"]
)
async def test_paths_outside_the_workspace_are_refused(ctx: ToolContext, bad: str) -> None:
    with pytest.raises(ws.WorkspaceError):
        ws.resolve_in(ctx.workspace, bad)


async def test_symlinks_are_refused_for_read_and_write(ctx: ToolContext, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    os.symlink(secret, ctx.workspace / "link.txt")
    os.symlink(tmp_path, ctx.workspace / "dirlink")
    with pytest.raises(ws.WorkspaceError, match="symlinks"):
        await call(ws.workspace_read, ws.ReadIn, ctx, path="link.txt")
    with pytest.raises(ws.WorkspaceError, match="symlinks"):
        await call(ws.workspace_write, ws.WriteIn, ctx, path="link.txt", content="x")
    with pytest.raises(ws.WorkspaceError, match="symlinks"):
        await call(ws.workspace_write, ws.WriteIn, ctx, path="dirlink/new.txt", content="x")
    assert secret.read_text() == "TOP SECRET"
    assert not (tmp_path / "new.txt").exists()


async def test_caps(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "MAX_FILES", 2)
    await call(ws.workspace_write, ws.WriteIn, ctx, path="1", content="a")
    await call(ws.workspace_write, ws.WriteIn, ctx, path="2", content="b")
    await call(ws.workspace_write, ws.WriteIn, ctx, path="1", content="overwrite is fine")
    with pytest.raises(ws.WorkspaceError, match="file limit"):
        await call(ws.workspace_write, ws.WriteIn, ctx, path="3", content="c")
    monkeypatch.setattr(ws, "MAX_READ_CHARS", 10)
    await call(ws.workspace_write, ws.WriteIn, ctx, path="1", content="x" * 50)
    out = await call(ws.workspace_read, ws.ReadIn, ctx, path="1")
    assert out.startswith("x" * 10) and out.endswith("[... file truncated]")
