"""`make reset-demo`: clear local runs so the UI starts clean for a demo. Dev only.

Deletes every task (and, by cascade, events, calls, approvals, side effects, budgets, jobs),
all memories, LangGraph checkpoints, task workspaces and Mailpit messages. Keeps users and
their tokens. Refused when ENV=prod: there these records are the audit trail.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys

import httpx
from sqlalchemy import text

from agentops.db.session import make_engine
from agentops.settings import Settings, get_settings

TABLES = (
    "tasks, memories, jobs, sse_tickets, rate_limit_buckets, "
    "checkpoints, checkpoint_blobs, checkpoint_writes"
)


def refuse_in_prod(settings: Settings) -> None:
    if settings.env == "prod":
        sys.exit("reset-demo is disabled when ENV=prod (audit records are never deleted)")


async def reset(settings: Settings) -> dict[str, int]:
    engine = make_engine(settings.sqlalchemy_url)
    try:
        async with engine.begin() as c:
            counts = {
                t: int((await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one())  # noqa: S608
                for t in ("tasks", "memories")
            }
            await c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()
    root = settings.workspace_root.resolve()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    try:
        async with httpx.AsyncClient(timeout=5) as h:
            await h.delete(f"{settings.mailpit_api_url.rstrip('/')}/api/v1/messages")
        counts["mailpit"] = 1
    except httpx.HTTPError:
        counts["mailpit"] = 0
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()
    settings = get_settings()
    refuse_in_prod(settings)
    if not args.yes:
        print("This deletes ALL local tasks, memories, workspaces and Mailpit messages.")
        print("Users and tokens are kept.")
        if input("Type 'reset' to continue: ").strip() != "reset":
            sys.exit("aborted; nothing deleted")
    counts = asyncio.run(reset(settings))
    print(
        f"cleared {counts['tasks']} tasks, {counts['memories']} memories, "
        f"workspaces, and Mailpit ({'ok' if counts['mailpit'] else 'not reachable'})"
    )


if __name__ == "__main__":
    main()
