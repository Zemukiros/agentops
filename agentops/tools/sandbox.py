"""Code sandbox (ADR-011): one throwaway container per run_python call.

Container settings: network_mode=none, uid 10001, cap_drop=ALL, no-new-privileges, read-only
root, tmpfs /tmp (64 MB), 512 MB memory (no extra swap), 1 CPU, 64 PIDs, an EMPTY environment
(only the image's own non-secret defaults), json-file logs capped at 1 MB, a wall-clock
timeout, then kill + forced removal in `finally`. Output is truncated.

Lifecycle uses only create -> start -> wait -> logs -> kill -> remove. Never exec: the socket
proxy blocks exec on purpose (ADR-011), and a test checks this module never calls it.

Workspace: the task's own directory is bind-mounted read-write at /work. The mount SOURCE is
the path as the Docker host sees it (WORKSPACE_HOST_ROOT/<task_id>), not the worker
container's path (WORKSPACE_ROOT/<task_id>). Phase 3 change 4.

Self-check (run inside the worker container, through the socket proxy):
    python -m agentops.tools.sandbox --selfcheck
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, NotFound
from docker.types import LogConfig
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from agentops.settings import Settings, get_settings

IMAGE = "agentops-sandbox:py312"
SANDBOX_UID = "10001:10001"
MAX_OUTPUT_CHARS = 20_000
LABEL = "agentops.sandbox"


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    oom_killed: bool
    duration_s: float
    truncated: bool


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) <= MAX_OUTPUT_CHARS:
        return s, False
    return s[:MAX_OUTPUT_CHARS] + f"\n[... output truncated: {len(s)} chars total]", True


def host_mount_source(settings: Settings, task_id: uuid.UUID) -> str:
    """The task workspace as the Docker HOST sees it (the bind-mount source)."""
    if not isinstance(task_id, uuid.UUID):
        raise ValueError("task_id must be a UUID")
    host_root = settings.workspace_host_root or settings.workspace_root.resolve()
    if not host_root.is_absolute():
        raise ValueError(f"WORKSPACE_HOST_ROOT must be absolute, got {host_root}")
    return str(host_root / str(task_id))


class Sandbox:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    def _docker(self) -> Any:
        if self._client is None:
            # DOCKER_HOST: the socket proxy in compose; the local daemon on a dev host / CI.
            self._client = docker.from_env(timeout=30)
        return self._client

    async def run(
        self, code: str, *, task_id: uuid.UUID, workspace: Path, timeout_s: float
    ) -> SandboxResult:
        return await asyncio.to_thread(self._run, code, task_id, workspace, timeout_s)

    def _run(
        self, code: str, task_id: uuid.UUID, workspace: Path, timeout_s: float
    ) -> SandboxResult:
        run_dir = workspace / ".run"
        run_dir.mkdir(exist_ok=True)
        os.chmod(run_dir, 0o777)  # noqa: S103  # the sandbox (uid 10001) may write here too
        script = run_dir / f"{uuid.uuid4().hex}.py"
        script.write_text(code)
        os.chmod(script, 0o644)
        client = self._docker()
        started = time.monotonic()
        container = client.containers.create(
            IMAGE,
            command=["python", "-I", "-B", f"/work/.run/{script.name}"],
            user=SANDBOX_UID,
            working_dir="/work",
            environment={},  # nothing from the worker; image defaults only
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            tmpfs={"/tmp": "size=64m,mode=1777"},  # noqa: S108  # tmpfs inside the container
            mem_limit="512m",
            memswap_limit="512m",
            nano_cpus=1_000_000_000,
            pids_limit=64,
            ipc_mode="private",
            volumes={host_mount_source(self._settings, task_id): {"bind": "/work", "mode": "rw"}},
            log_config=LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
            labels={LABEL: "1", "agentops.task": str(task_id)},
            detach=True,
        )
        timed_out = False
        exit_code: int | None = None
        oom = False
        try:
            container.start()
            try:
                status = container.wait(timeout=timeout_s)
                exit_code = int(status.get("StatusCode", -1))
            except (ReadTimeout, RequestsConnectionError):
                timed_out = True
                with contextlib.suppress(DockerException):
                    container.kill()
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
            try:
                container.reload()
                oom = bool(container.attrs.get("State", {}).get("OOMKilled"))
            except DockerException:
                pass
        finally:
            with contextlib.suppress(NotFound):
                container.remove(force=True)
            script.unlink(missing_ok=True)
        out, t1 = _truncate(stdout)
        err, t2 = _truncate(stderr)
        return SandboxResult(
            exit_code=exit_code,
            stdout=out,
            stderr=err,
            timed_out=timed_out,
            oom_killed=oom,
            duration_s=round(time.monotonic() - started, 3),
            truncated=t1 or t2,
        )


def _selfcheck() -> int:
    """Through-proxy check (Phase 3 change 4): write a file in a task workspace, run code that
    reads it and writes another file, and read both the output and the new file back."""
    from agentops.tools.native.workspace import task_workspace  # noqa: PLC0415

    settings = get_settings()
    tid = uuid.uuid4()
    ws = task_workspace(settings, tid)
    (ws / "input.txt").write_text("hello from the worker")
    code = (
        "import os\n"
        "data = open('/work/input.txt').read()\n"
        "open('/work/output.txt', 'w').write(data.upper())\n"
        "print('read:', data, '| uid:', os.getuid(), '| env keys:', sorted(os.environ))\n"
    )
    res = Sandbox(settings)._run(code, tid, ws, 30)
    back = (ws / "output.txt").read_text() if (ws / "output.txt").exists() else None
    print(f"mount source (host view): {host_mount_source(settings, tid)}")
    print(f"workspace (worker view):  {ws}")
    print(f"exit={res.exit_code} timed_out={res.timed_out} stdout={res.stdout.strip()!r}")
    print(f"stderr={res.stderr.strip()[:300]!r}")
    print(f"file written by the sandbox, read back by the worker: {back!r}")
    ok = res.exit_code == 0 and back == "HELLO FROM THE WORKER"
    print("SELFCHECK", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--selfcheck", action="store_true")
    if p.parse_args().selfcheck:
        sys.exit(_selfcheck())
    p.print_help()
