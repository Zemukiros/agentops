"""Sandbox isolation (ADR-011), against a real Docker daemon. Marker `sandbox` (own CI job).

Proves: no network, no secrets from the worker's environment, infinite loop killed and the
container removed, fork bomb contained by the PID limit, output truncated, non-root, read-only
root, the task workspace mounted from the HOST path, and that this module never uses exec.
"""

import ast
import inspect
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from agentops.settings import Settings
from agentops.tools import sandbox as sb
from agentops.tools.native.workspace import task_workspace

pytestmark = pytest.mark.sandbox


@pytest.fixture(scope="module")
def docker_client() -> Iterator[docker.DockerClient]:
    try:
        client = docker.from_env(timeout=30)
        client.images.get(sb.IMAGE)
    except Exception as exc:
        if os.environ.get("AGENTOPS_REQUIRE_SANDBOX") == "1":
            pytest.fail(f"sandbox prerequisites missing: {exc}")
        pytest.skip(f"Docker or {sb.IMAGE} unavailable ({exc}); run `make build-sandbox`")
    yield client
    client.close()


@pytest.fixture
def env(tmp_path: Path, docker_client: docker.DockerClient) -> tuple[sb.Sandbox, uuid.UUID, Path]:
    settings = Settings(workspace_root=tmp_path / "ws")
    tid = uuid.uuid4()
    return sb.Sandbox(settings, docker_client), tid, task_workspace(settings, tid)


def run(
    env: tuple[sb.Sandbox, uuid.UUID, Path], code: str, timeout: float = 20
) -> sb.SandboxResult:
    box, tid, ws = env
    return box._run(code, tid, ws, timeout)


def leftover(client: docker.DockerClient, tid: uuid.UUID) -> list[object]:
    return client.containers.list(all=True, filters={"label": f"agentops.task={tid}"})


def test_reads_and_writes_the_task_workspace(env, docker_client) -> None:
    _, tid, ws = env
    (ws / "input.txt").write_text("hello")
    res = run(env, "d=open('input.txt').read(); open('out.txt','w').write(d*2); print(d)")
    assert (res.exit_code, res.stdout.strip()) == (0, "hello")
    assert (ws / "out.txt").read_text() == "hellohello"
    assert leftover(docker_client, tid) == []
    assert not list((ws / ".run").iterdir())  # the script is cleaned up


def test_runs_as_non_root_with_a_read_only_root(env) -> None:
    code = (
        "import os\n"
        "print(os.getuid(), os.getgid())\n"
        "try:\n    open('/usr/local/pwned', 'w')\nexcept OSError as e:\n    print('ro', e.errno)\n"
        "open('/tmp/ok', 'w').write('x'); print('tmp ok')\n"
    )
    out = run(env, code).stdout.split("\n")
    assert out[0] == "10001 10001" and out[1].startswith("ro ") and out[2] == "tmp ok"


def test_no_network(env) -> None:
    code = (
        "import socket\n"
        "for f in (lambda: socket.create_connection(('1.1.1.1', 53), timeout=3),\n"
        "          lambda: socket.getaddrinfo('example.com', 443)):\n"
        "    try:\n        f(); print('CONNECTED')\n"
        "    except OSError as e:\n        print('blocked', type(e).__name__)\n"
        "print(sorted(n for _, n in socket.if_nameindex()))\n"
    )
    res = run(env, code)
    assert "CONNECTED" not in res.stdout
    assert res.stdout.count("blocked") == 2
    assert res.stdout.strip().endswith("['lo']")  # loopback only


def test_worker_secrets_never_reach_the_sandbox(env, monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "canary-" + uuid.uuid4().hex
    monkeypatch.setenv("AGENTOPS_CANARY_SECRET", canary)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + canary)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:{canary}@db/x")
    res = run(env, "import os, json; print(json.dumps(dict(os.environ)))")
    assert canary not in res.stdout and canary not in res.stderr
    keys = set(json.loads(res.stdout))
    assert not keys & {"AGENTOPS_CANARY_SECRET", "ANTHROPIC_API_KEY", "DATABASE_URL"}
    assert keys <= {
        "PATH",
        "HOME",
        "HOSTNAME",
        "LANG",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PIP_NO_CACHE_DIR",
    }, keys


def test_infinite_loop_is_killed_and_removed(env, docker_client) -> None:
    _, tid, _ = env
    res = run(env, "while True:\n    pass\n", timeout=2)
    assert res.timed_out and res.duration_s < 15
    assert leftover(docker_client, tid) == []


def test_fork_bomb_is_contained(env, docker_client) -> None:
    _, tid, _ = env
    counted = run(
        env,
        "import os, time\n"
        "n = 0\n"
        "for _ in range(500):\n"
        "    try:\n        pid = os.fork()\n"
        "    except OSError:\n        break\n"
        "    if pid == 0:\n        time.sleep(30); os._exit(0)\n"
        "    n += 1\n"
        "print(n)\n",
        timeout=15,
    )
    assert counted.exit_code == 0 and int(counted.stdout.strip()) < 64
    bomb = run(
        env,
        "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass\n",
        timeout=3,
    )
    assert bomb.timed_out
    assert leftover(docker_client, tid) == []
    # the host is fine: a normal run right after works
    assert run(env, "print('alive')").stdout.strip() == "alive"


def test_output_is_truncated(env) -> None:
    res = run(env, "print('x' * 200_000)")
    assert res.truncated and len(res.stdout) < sb.MAX_OUTPUT_CHARS + 200
    assert "output truncated" in res.stdout


def test_mount_source_is_the_host_path_not_the_worker_path() -> None:
    s = Settings(workspace_root=Path("/workspaces"), workspace_host_root=Path("/srv/agentops/ws"))
    tid = uuid.uuid4()
    assert sb.host_mount_source(s, tid) == f"/srv/agentops/ws/{tid}"
    with pytest.raises(ValueError, match="absolute"):
        sb.host_mount_source(Settings(workspace_host_root=Path("rel/ws")), tid)


def test_sandbox_module_never_uses_exec() -> None:
    tree = ast.parse(inspect.getsource(sb))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not {a for a in attrs if "exec" in a.lower()}, attrs
