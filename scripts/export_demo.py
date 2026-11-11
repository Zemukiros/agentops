"""Export one finished task from a running local stack as the static demo's recording.

    uv run python scripts/export_demo.py <task_id> [ui/src/demo/recording.json]

Reads only through the public API (the same JSON the UI renders), with the seeded tokens in
.agentops-tokens: task detail, the full event stream, trace, cost, the task's approvals and a
few memories. Everything then goes through the trace redactor (ADR-042) a second time, and
internal details are replaced: user ids become the demo identities, Mailpit links are dropped.
The output is what ui/src/demo/ plays back (ADR-053). Free: no model calls.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from agentops.observability.redact import redact

API = "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "ui/src/demo/recording.json"
MEMORY_LIMIT = 8


def tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".agentops-tokens").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get(c: httpx.Client, path: str, token: str, **params: Any) -> Any:
    r = c.get(path, headers={"Authorization": f"Bearer {token}"}, params=params)
    r.raise_for_status()
    return r.json()


def events(c: httpx.Client, task_id: str, token: str) -> list[dict[str, Any]]:
    """The full stream from event 0; the server ends it after task.finished."""
    t = c.post(f"/tasks/{task_id}/stream-ticket", headers={"Authorization": f"Bearer {token}"})
    t.raise_for_status()
    out: list[dict[str, Any]] = []
    with c.stream(
        "GET", f"/tasks/{task_id}/events", params={"ticket": t.json()["ticket"], "after": 0}
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            out.append(ev)
            if ev["type"] == "task.finished":
                break
    return out


def trace(c: httpx.Client, task_id: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: Any = None
    while True:
        page = get(
            c, f"/tasks/{task_id}/trace", token, limit=500, **({"cursor": cursor} if cursor else {})
        )
        items += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def scrub(value: Any, ids: dict[str, str]) -> Any:
    """Replace internal ids, worker names and local links anywhere in the recording."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "mailpit_url":
                out[k] = None
            elif k == "worker" and isinstance(v, str):
                out[k] = "worker-1"  # container id + process: internal
            else:
                out[k] = scrub(v, ids)
        return out
    if isinstance(value, list):
        return [scrub(v, ids) for v in value]
    if isinstance(value, str):
        for real, alias in ids.items():
            value = value.replace(real, alias)
        return re.sub(r"https?://(localhost|127\.0\.0\.1)(:\d+)?\S*", "[local link]", value)
    return value


def main() -> None:
    task_id = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    tok = tokens()
    user, reviewer = tok["demo-user"], tok["demo-reviewer"]
    with httpx.Client(base_url=API, timeout=30) as c:
        me_user = get(c, "/me", user)
        me_reviewer = get(c, "/me", reviewer)
        detail = get(c, f"/tasks/{task_id}/detail", user)
        listing = get(c, "/tasks", user, limit=100)
        item = next(i for i in listing["items"] if i["id"] == task_id)
        approvals = [get(c, f"/approvals/{a['id']}", reviewer) for a in detail["approvals"]]
        recording = {
            "source": {
                "task_id": task_id,
                "note": "Exported from a real-model run by scripts/export_demo.py (ADR-053).",
            },
            "identities": [
                {"id": "demo-user", "name": me_user["name"], "role": me_user["role"]},
                {"id": "demo-reviewer", "name": me_reviewer["name"], "role": me_reviewer["role"]},
            ],
            "task": item,
            "detail": detail,
            "events": events(c, task_id, user),
            "trace": trace(c, task_id, user),
            "cost": get(c, f"/tasks/{task_id}/cost", user),
            "approvals": approvals,
            "memories": get(c, "/memories", user, limit=MEMORY_LIMIT, offset=0),
        }
    ids = {me_user["id"]: "demo-user", me_reviewer["id"]: "demo-reviewer"}
    clean = scrub(redact(recording, max_chars=100_000), ids)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(clean, indent=1, ensure_ascii=False) + "\n")
    n = {k: len(v) for k, v in clean.items() if isinstance(v, list)}
    print(f"wrote {out} ({out.stat().st_size:,} bytes) {n}")


if __name__ == "__main__":
    main()
