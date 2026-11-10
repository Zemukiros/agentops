"""report.md + results.json + spotcheck.md for one eval run. Only measured numbers."""

from __future__ import annotations

import json
import random
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentops.settings import Settings
from evals.judge import JUDGE_EFFORT, JUDGE_MODEL, PROMPT_VERSION
from evals.metrics import paired_lift, percentile, precision_recall, wilson

ROLES = ("supervisor", "reviewer", "research", "analysis", "writing", "code")


def git_commit() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
        ).stdout
        return sha + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _rate(rows: list[dict[str, Any]], key: str = "passed") -> dict[str, Any]:
    n = len(rows)
    s = sum(1 for r in rows if r.get(key))
    lo, hi = wilson(s, n)
    return {"n": n, "pass": s, "rate": s / n if n else None, "ci95": [lo, hi]}


def summarize(
    records: list[dict[str, Any]], settings: Settings, meta: dict[str, Any]
) -> dict[str, Any]:
    ran = [r for r in records if "skipped" not in r]
    agentic = [r for r in ran if r.get("kind") in ("agentic", None) and "error" not in r]
    # Errored runs (harness or provider errors, e.g. an account usage limit) are not model
    # results: they are listed under errors and excluded from every rate.
    scored = [r for r in ran if "error" not in r]
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scored:
        if r.get("kind") == "memory_ab" and r.get("arm") == "off":
            continue  # the off arm is the control, not a scenario result
        by_cat[r.get("category", "?")].append(r)
    reviewer = [r for r in ran if r.get("kind") == "reviewer" and "error" not in r]
    mem_pairs: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    for r in scored:
        if r.get("kind") == "memory_ab" and "arm" in r:
            mem_pairs[(r["scenario"], r["rep"])][r["arm"]] = 1.0 if r.get("passed") else 0.0
    pairs = [(v["on"], v["off"]) for v in mem_pairs.values() if "on" in v and "off" in v]
    costs = [r["cost_usd"] for r in agentic if "cost_usd" in r]
    lats = [r["latency_s"] for r in agentic if r.get("latency_s")]
    total_cost = sum(r.get("cost_usd", 0) + r.get("prime_cost_usd", 0) for r in ran)
    judge_cost = sum(r.get("judge_cost_usd", 0) for r in ran)
    return {
        "meta": meta
        | {
            "git_commit": git_commit(),
            "finished_at": datetime.now(UTC).isoformat(),
            "role_models": {
                role: f"{settings.model_for(role).provider}/{settings.model_for(role).model}"
                f" (effort {getattr(settings.model_for(role), 'effort', None)})"
                for role in ROLES
            },
            "judge": {
                "model": meta.get("judge", JUDGE_MODEL),
                "effort": JUDGE_EFFORT,
                "prompt_version": PROMPT_VERSION,
                "bias_note": None
                if meta.get("judge", JUDGE_MODEL) == JUDGE_MODEL
                else "the judge is the same model as the system's planner and reviewer "
                "(claude-sonnet-5-5): possible self-grading bias; see docs/EVALUATION.md",
            },
            "fingerprints": sorted(
                {(r["scenario"], r.get("fingerprint")) for r in ran if "scenario" in r}
            ),
        },
        "overall": _rate(
            [r for r in scored if not (r.get("kind") == "memory_ab" and r.get("arm") == "off")]
        ),
        "errored_runs": len(ran) - len(scored),
        "by_category": {c: _rate(rs) for c, rs in sorted(by_cat.items())},
        "cost_usd": {
            "p50": percentile(costs, 50),
            "p95": percentile(costs, 95),
            "runs": len(costs),
        },
        "latency_s": {"p50": percentile(lats, 50), "p95": percentile(lats, 95), "runs": len(lats)},
        "escalation_rate": _rate(agentic, "escalated"),
        "loop_rate": _rate(scored, "loop"),
        "unsafe_actions": sum(1 for r in ran if r.get("unsafe")),
        "reviewer": precision_recall(
            [(r["predicted_defect"], r["actual_defect"]) for r in reviewer]
        ),
        "memory_lift": paired_lift(pairs),
        "errors": [
            {"scenario": r.get("scenario"), "rep": r.get("rep"), "error": r["error"]}
            for r in ran
            if "error" in r
        ],
        "skipped": [
            {"scenario": r["scenario"], "rep": r["rep"], "why": r["skipped"]}
            for r in records
            if "skipped" in r
        ],
        "spend": {
            "agents_usd": round(total_cost, 6),
            "judge_usd": round(judge_cost, 6),
            "total_usd": round(total_cost + judge_cost, 6),
        },
    }


def fmt(v: float | None, f: str) -> str:
    return "n/a" if v is None else f.format(v)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _ci(r: dict[str, Any]) -> str:
    lo, hi = r["ci95"]
    return f"{_pct(r['rate'])} ({r['pass']}/{r['n']}; 95% CI {_pct(lo)}–{_pct(hi)})"


def render(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    m = summary["meta"]
    lines = [
        f"# Eval report: {m['run_id']}",
        "",
        f"- Mode: **{m['mode']}** · k = {m['k']} · scenarios = {m['scenarios']} · cap ${m['cap_usd']}",
        f"- Git commit: `{m['git_commit']}` · finished {m['finished_at']}",
        f"- Judge: `{m['judge']['model']}` (effort {m['judge']['effort']}, prompt `{m['judge']['prompt_version']}`)"
        + (f" - **{m['judge']['bias_note']}**" if m["judge"].get("bias_note") else ""),
        "- Role models: " + ", ".join(f"{k} `{v}`" for k, v in m["role_models"].items()),
        f"- Spend: agents ${summary['spend']['agents_usd']:.4f} + judge ${summary['spend']['judge_usd']:.4f} = **${summary['spend']['total_usd']:.4f}**",
        "",
        "A run passes only if every trajectory check passes and the judge passes every rubric",
        "criterion. Intervals are Wilson 95%.",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Pass rate (all scenarios) | {_ci(summary['overall'])} |",
        f"| Errored runs (excluded from rates; see below) | {summary.get('errored_runs', 0)} |",
        f"| Unsafe actions (must be 0) | **{summary['unsafe_actions']}** |",
        f"| Reviewer precision / recall | {_pct(summary['reviewer']['precision'])} / {_pct(summary['reviewer']['recall'])} (tp {summary['reviewer']['tp']}, fp {summary['reviewer']['fp']}, fn {summary['reviewer']['fn']}, tn {summary['reviewer']['tn']}) |",
        f"| Escalation rate (agentic runs) | {_ci(summary['escalation_rate'])} |",
        f"| Loop rate (cap hit or no clean finish) | {_ci(summary['loop_rate'])} |",
    ]
    lift = summary["memory_lift"]
    lines.append(
        "| Memory lift (pass rate, on − off) | "
        + (
            "n/a"
            if lift["lift"] is None
            else f"{100 * lift['lift']:+.1f} pts (n={lift['n']} pairs; bootstrap 95% CI {100 * lift['low']:+.1f} to {100 * lift['high']:+.1f})"
        )
        + " |"
    )
    c, la = summary["cost_usd"], summary["latency_s"]
    lines += [
        f"| Cost per agentic run p50 / p95 | {fmt(c['p50'], '${:.4f}')} / {fmt(c['p95'], '${:.4f}')} |",
        f"| Latency per agentic run p50 / p95 | {fmt(la['p50'], '{:.1f} s')} / {fmt(la['p95'], '{:.1f} s')} |",
        "",
        "## By category",
        "",
        "| Category | Pass rate |",
        "|---|---|",
    ]
    lines += [f"| {cat} | {_ci(r)} |" for cat, r in summary["by_category"].items()]
    lines += [
        "",
        "## Runs",
        "",
        "| Scenario | Rep | Arm | Result | Failed checks | Judge | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(
        records, key=lambda x: (x.get("scenario", ""), x.get("rep", 0), x.get("arm", ""))
    ):
        if "skipped" in r:
            lines.append(f"| {r['scenario']} | {r['rep']} | | skipped | {r['skipped'][:60]} | | |")
            continue
        failed = ", ".join(
            f"{c['name']} ({c['detail'][:50]})" for c in r.get("checks", []) if not c["pass"]
        ) or ("error: " + r["error"][:60] if "error" in r else "")
        j = r.get("judge")
        jtxt = (
            ""
            if not j
            else (
                "pass"
                if j["pass"]
                else "fail: "
                + "; ".join(c["criterion"][:40] for c in j["criteria"] if not c["pass"])
            )
        )
        lines.append(
            f"| {r.get('scenario')} | {r.get('rep')} | {r.get('arm', '')} | {'PASS' if r.get('passed') else 'FAIL'} | {failed} | {jtxt} | ${r.get('cost_usd', 0):.4f} |"
        )
    if summary["errors"] or summary["skipped"]:
        lines += ["", "## Errors and skipped runs", ""]
        lines += [f"- error {e['scenario']}#{e['rep']}: {e['error']}" for e in summary["errors"]]
        lines += [f"- skipped {s['scenario']}#{s['rep']}: {s['why']}" for s in summary["skipped"]]
    return "\n".join(lines) + "\n"


def spotcheck(records: list[dict[str, Any]], *, seed: int = 20261110, frac: float = 0.10) -> str:
    judged = [r for r in records if r.get("judge")]
    rng = random.Random(seed)
    n = max(1, round(frac * len(judged))) if judged else 0
    sample = rng.sample(judged, n) if n else []
    out = [
        "# Judge spot-check sample",
        "",
        f"A seeded random {int(frac * 100)}% of judged runs ({n} of {len(judged)}). For each: the exact judge",
        "input and its per-criterion verdicts. Mark each verdict you disagree with.",
        "",
    ]
    for r in sample:
        out += [
            f"## {r['scenario']} rep {r['rep']} {r.get('arm', '')} (task {r['task_id']})",
            "",
            "```",
            r["judge"]["prompt"],
            "```",
            "",
        ]
        out += [
            f"- [{'x' if c['pass'] else ' '}] {c['criterion']}: {c['reason']}"
            for c in r["judge"]["criteria"]
        ]
        out += ["", "Your verdict: agree / disagree on criteria: ___", ""]
    return "\n".join(out) + "\n"


def write(
    out_dir: Path, records: list[dict[str, Any]], settings: Settings, meta: dict[str, Any]
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(records, settings, meta)
    slim = [
        {k: v for k, v in r.items() if k != "judge"}
        | (
            {"judge": {k: v for k, v in r["judge"].items() if k != "prompt"}}
            if r.get("judge")
            else {}
        )
        for r in records
    ]
    (out_dir / "results.json").write_text(
        json.dumps({"summary": summary, "runs": slim}, indent=1, default=str) + "\n"
    )
    (out_dir / "report.md").write_text(render(summary, records))
    (out_dir / "spotcheck.md").write_text(spotcheck(records))
    return summary
