"""Per-task workspace tools (ADR-008): read, write, list inside the task's own directory.

Every path is relative to the task workspace. It is rejected if it is absolute, escapes the
workspace after normalization, or passes through a symlink at any level (a symlink planted by
generated code in the sandbox must not redirect a later read/write outside the workspace).
Writes use O_NOFOLLOW. Caps: 1 MB per file, 200 files, 20 MB per workspace; reads return at
most 64 KB of text.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from pydantic import Field

from agentops.settings import Settings
from agentops.tools.spec import ALL_SPECIALISTS, ToolContext, ToolInput, ToolOutput, ToolSpec

MAX_FILE_BYTES = 1_000_000
MAX_FILES = 200
MAX_TOTAL_BYTES = 20_000_000
MAX_READ_CHARS = 64_000
MAX_LIST = 500


class WorkspaceError(ValueError):
    pass


def task_workspace(settings: Settings, task_id: uuid.UUID) -> Path:
    """Create (if needed) and return this task's workspace dir, as this process sees it."""
    root = settings.workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    ws = root / str(task_id)
    ws.mkdir(exist_ok=True)
    # The sandbox runs as uid 10001 and must read and write here; nothing else is mounted in.
    os.chmod(ws, 0o777)  # noqa: S103
    return ws


def resolve_in(workspace: Path, rel: str) -> Path:
    if not rel or "\x00" in rel:
        raise WorkspaceError("path must be a non-empty relative path")
    if os.path.isabs(rel) or rel.startswith("~"):
        raise WorkspaceError(f"absolute paths are not allowed: {rel!r}")
    base = workspace.resolve()
    candidate = Path(os.path.normpath(base / rel))
    if candidate != base and base not in candidate.parents:
        raise WorkspaceError(f"path escapes the workspace: {rel!r}")
    # Refuse symlinks anywhere between the workspace and the target.
    cur = base
    for part in candidate.relative_to(base).parts:
        cur = cur / part
        if cur.is_symlink():
            raise WorkspaceError(f"symlinks are not allowed: {rel!r}")
    return candidate


def _usage(workspace: Path) -> tuple[int, int]:
    files = total = 0
    for p in workspace.rglob("*"):
        if p.is_file() and not p.is_symlink():
            files += 1
            total += p.stat().st_size
    return files, total


class ReadIn(ToolInput):
    path: str = Field(min_length=1, max_length=300, description="file path inside the workspace")


class WriteIn(ToolInput):
    path: str = Field(min_length=1, max_length=300, description="file path inside the workspace")
    content: str = Field(max_length=MAX_FILE_BYTES, description="UTF-8 text to write")


class ListIn(ToolInput):
    path: str = Field(default=".", max_length=300, description="directory inside the workspace")


async def workspace_read(args: ReadIn, ctx: ToolContext) -> ToolOutput:
    target = resolve_in(ctx.workspace, args.path)
    if not target.is_file():
        raise WorkspaceError(f"no such file: {args.path}")
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        raw = f.read(MAX_READ_CHARS * 4 + 1)
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n[... file truncated]"
    return ToolOutput(text=text, data={"path": args.path, "bytes": target.stat().st_size})


async def workspace_write(args: WriteIn, ctx: ToolContext) -> ToolOutput:
    data = args.content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceError(f"file too large ({len(data)} bytes > {MAX_FILE_BYTES})")
    target = resolve_in(ctx.workspace, args.path)
    if target == ctx.workspace.resolve():
        raise WorkspaceError("path must name a file")
    files, total = _usage(ctx.workspace)
    existing = target.stat().st_size if target.is_file() else 0
    if not target.exists() and files >= MAX_FILES:
        raise WorkspaceError(f"workspace file limit reached ({MAX_FILES})")
    if total - existing + len(data) > MAX_TOTAL_BYTES:
        raise WorkspaceError("workspace size limit reached")
    # create parents one level at a time, refusing symlinks (resolve_in already checked them)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return ToolOutput(text=f"wrote {len(data)} bytes to {args.path}", data={"path": args.path})


async def workspace_list(args: ListIn, ctx: ToolContext) -> ToolOutput:
    target = resolve_in(ctx.workspace, args.path or ".")
    if not target.is_dir():
        raise WorkspaceError(f"no such directory: {args.path}")
    lines = []
    for i, entry in enumerate(sorted(target.iterdir())):
        if i >= MAX_LIST:
            lines.append(f"[... more than {MAX_LIST} entries]")
            break
        st = entry.lstat()
        kind = "link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file"
        lines.append(f"{kind}\t{st.st_size}\t{entry.relative_to(ctx.workspace.resolve())}")
    return ToolOutput(text="\n".join(lines) or "(empty)")


WORKSPACE_TOOLS = [
    ToolSpec(
        name="workspace_read",
        description="Read a UTF-8 text file from this task's workspace.",
        input_model=ReadIn,
        fn=workspace_read,
        allowed_agents=ALL_SPECIALISTS,
        risk="read",
        scope="task_workspace",
        timeout_s=10,
        rate_per_min=120,
    ),
    ToolSpec(
        name="workspace_write",
        description="Write a UTF-8 text file in this task's workspace (creates or overwrites).",
        input_model=WriteIn,
        fn=workspace_write,
        allowed_agents=ALL_SPECIALISTS,
        risk="write",
        scope="task_workspace",
        timeout_s=10,
        rate_per_min=60,
    ),
    ToolSpec(
        name="workspace_list",
        description="List files in a directory of this task's workspace.",
        input_model=ListIn,
        fn=workspace_list,
        allowed_agents=ALL_SPECIALISTS,
        risk="read",
        scope="task_workspace",
        timeout_s=10,
        rate_per_min=120,
    ),
]
