"""run_python: execute generated code in the Docker sandbox (ADR-011). Never in the worker."""

from __future__ import annotations

from pydantic import Field

from agentops.tools.sandbox import Sandbox
from agentops.tools.spec import ToolContext, ToolInput, ToolOutput, ToolSpec

MAX_CODE_CHARS = 100_000


class RunPythonIn(ToolInput):
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS, description="Python 3.12 source")
    timeout_s: int = Field(default=10, ge=1, le=30, description="wall-clock limit in seconds")


def _listing(before: set[str], after: set[str]) -> str:
    new = sorted(after - before)
    return ("\nfiles created in the workspace: " + ", ".join(new)) if new else ""


def _files(ctx: ToolContext) -> set[str]:
    base = ctx.workspace
    return {
        str(p.relative_to(base))
        for p in base.rglob("*")
        if p.is_file() and not p.is_symlink() and ".run" not in p.parts
    }


async def run_python(args: RunPythonIn, ctx: ToolContext) -> ToolOutput:
    before = _files(ctx)
    res = await Sandbox(ctx.settings).run(
        args.code, task_id=ctx.task_id, workspace=ctx.workspace, timeout_s=args.timeout_s
    )
    status = (
        f"killed after {args.timeout_s}s timeout"
        if res.timed_out
        else "killed: out of memory"
        if res.oom_killed
        else f"exit code {res.exit_code}"
    )
    text = (
        f"{status} ({res.duration_s}s)\n--- stdout ---\n{res.stdout}"
        + (f"\n--- stderr ---\n{res.stderr}" if res.stderr.strip() else "")
        + _listing(before, _files(ctx))
    )
    return ToolOutput(
        text=text,
        data={"exit_code": res.exit_code, "timed_out": res.timed_out, "oom": res.oom_killed},
    )


PYTHON_TOOLS = [
    ToolSpec(
        name="run_python",
        description=(
            "Run Python 3.12 in an isolated sandbox: no network, no secrets, numpy and pandas "
            "installed. The task workspace is the current directory (/work); files you write "
            "there persist for later steps. Print what you need to see."
        ),
        input_model=RunPythonIn,
        fn=run_python,
        allowed_agents=frozenset({"analysis", "code"}),
        risk="write",
        scope="sandbox",
        timeout_s=45,  # registry timeout > the sandbox's own limit (max 30 s) + startup
        rate_per_min=20,
    ),
]
