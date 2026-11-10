"""python -m evals {estimate,run,replay}

estimate                     cost estimate (free; uses count_tokens if an API key is set)
run --mode dry               all scenarios with scripted models + fake judge ($0)
run --mode real --yes        the paid run (refuses without --yes; hard cap enforced)
replay                       recorded cassettes, compared with their recorded outcome ($0)
tables [--check]             regenerate (or check) the doc tables from committed results ($0)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agentops.settings import get_settings
from evals import cassette, estimate, report, runner, tables
from evals.judge import JUDGE_MODELS
from evals.spec import Scenario, load

RESULTS = Path(__file__).parent / "results"


def _select(args: argparse.Namespace) -> list[Scenario]:
    scenarios = load()
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s.id in wanted]
    if args.category:
        cats = set(args.category.split(","))
        scenarios = [s for s in scenarios if s.category in cats]
    return scenarios


async def _estimate(args: argparse.Namespace) -> None:
    client = None
    if os.environ.get("ANTHROPIC_API_KEY") or get_settings().anthropic_api_key:
        import anthropic  # noqa: PLC0415

        key = get_settings().anthropic_api_key
        client = anthropic.AsyncAnthropic(api_key=key.get_secret_value() if key else None)
    model = JUDGE_MODELS[args.judge]
    lines = await estimate.estimate(_select(args), args.k, client, judge=model)
    print(estimate.render(lines, args.k, args.cap, judge=model))
    print(
        "judge input tokens:",
        "counted with count_tokens (free)" if client else "approximated (no API key)",
    )


async def _run(args: argparse.Namespace) -> int:
    scenarios = _select(args)
    if args.mode == "real" and not args.yes:
        print(
            "refusing a paid run without --yes (show `python -m evals estimate` to the owner first)"
        )
        return 2
    if busy := runner.other_workers_running():
        print(
            f"refusing to run: another worker is consuming jobs ({', '.join(busy)}). "
            "Stop it first: docker compose stop worker"
        )
        return 3
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    out = Path(args.out) if args.out else RESULTS / f"{stamp}-{args.mode}"
    print(f"{len(scenarios)} scenarios x k={args.k}, mode {args.mode}, cap ${args.cap} -> {out}")
    records, _budget = await runner.run_all(
        scenarios,
        base=get_settings(),
        mode=args.mode,
        k=args.k,
        cap_usd=args.cap,
        concurrency=args.concurrency,
        out_dir=out,
        judge_model=JUDGE_MODELS[args.judge],
        record_cassettes=args.record_cassettes,
    )
    summary = report.write(
        out,
        records,
        runner.eval_settings(get_settings(), args.mode),
        {
            "run_id": out.name,
            "mode": args.mode,
            "k": args.k,
            "scenarios": len(scenarios),
            "cap_usd": args.cap,
            "label": args.label,
        },
    )
    print(
        f"\npass {summary['overall']['pass']}/{summary['overall']['n']}, "
        f"unsafe {summary['unsafe_actions']}, spend ${summary['spend']['total_usd']:.4f}"
    )
    print(f"report: {out / 'report.md'}")
    return 0


async def _replay(args: argparse.Namespace) -> int:
    expected = cassette.expected()
    if not expected:
        print("no cassettes recorded yet")
        return 0
    if busy := runner.other_workers_running():
        print(f"refusing to replay: another worker is consuming jobs ({', '.join(busy)})")
        return 3
    scenarios = [s for s in load() if s.id in expected]
    out = Path(args.out) if args.out else RESULTS / "replay"
    records, _ = await runner.run_all(
        scenarios, base=get_settings(), mode="replay", k=1, cap_usd=0, concurrency=2, out_dir=out
    )
    bad = []
    for r in records:
        exp = expected[r["scenario"]]["expected"]["checks"]
        got = {c["name"]: c["pass"] for c in r.get("checks", [])}
        if got != exp:
            bad.append((r["scenario"], exp, got, r.get("error")))
    for b in bad:
        print("MISMATCH", *b)
    print(f"replayed {len(records)} cassettes, {len(bad)} mismatches")
    return 1 if bad else 0


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m evals")
    sub = p.add_subparsers(dest="cmd", required=True)
    tp = sub.add_parser("tables")
    tp.add_argument("--check", action="store_true", help="exit 1 if a doc table is stale")
    for name in ("estimate", "run", "replay"):
        sp = sub.add_parser(name)
        sp.add_argument("--k", type=int, default=3)
        sp.add_argument("--cap", type=float, default=15.0)
        sp.add_argument("--only", help="comma-separated scenario ids")
        sp.add_argument("--category", help="comma-separated categories")
        sp.add_argument("--out")
        sp.add_argument("--judge", choices=sorted(JUDGE_MODELS), default="opus")
        if name == "run":
            sp.add_argument("--mode", choices=["dry", "real"], default="dry")
            sp.add_argument("--yes", action="store_true", help="confirm a paid (real) run")
            sp.add_argument("--concurrency", type=int, default=4)
            sp.add_argument("--label", default="", help="e.g. baseline / after-fixes")
            sp.add_argument(
                "--record-cassettes",
                action="store_true",
                help="overwrite existing cassettes (default: record only missing ones)",
            )
    args = p.parse_args()
    if args.cmd == "tables":
        if args.check:
            bad = tables.stale()
            print("stale doc tables: " + ", ".join(bad) if bad else "doc tables up to date")
            sys.exit(1 if bad else 0)
        print("rewrote: " + (", ".join(tables.write()) or "nothing (up to date)"))
        return
    if args.cmd == "estimate":
        asyncio.run(_estimate(args))
        return
    code = asyncio.run(_run(args) if args.cmd == "run" else _replay(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
