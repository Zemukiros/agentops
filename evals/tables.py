"""Doc tables generated from the committed eval results, never typed by hand.

`python -m evals tables` rewrites the marked blocks in README.md and docs/EVALUATION.md;
`python -m evals tables --check` (and a unit test) fail when a block no longer matches the data.

Inputs (committed under evals/results/):
- BASELINE: the first paid run, plus FILL, the separate run of the 3 runs the baseline's
  worst-case reservation skipped (same code, same scenarios). Together: the baseline.
- AFTER: the re-measure after the ADR-047..049 fixes. Runs that errored (provider usage limit)
  are not model results: excluded from rates and counted separately.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.metrics import paired_lift, percentile, precision_recall, wilson

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).parent / "results"
BASELINE = RESULTS / "2026-11-10-163339-real"
FILL = RESULTS / "2026-11-10-172318-real"
AFTER = RESULTS / "2026-11-10-173107-real"
TARGETS = {
    ROOT / "README.md": ("headline",),
    ROOT / "docs" / "EVALUATION.md": ("headline", "categories", "compare"),
}


def _runs(d: Path) -> list[dict[str, Any]]:
    return list(json.loads((d / "results.json").read_text())["runs"])


def baseline_runs() -> list[dict[str, Any]]:
    fill = [r for r in _runs(FILL) if "skipped" not in r]
    filled = {(r["scenario"], r["rep"]) for r in fill}
    base = [
        r for r in _runs(BASELINE) if not ("skipped" in r and (r["scenario"], r["rep"]) in filled)
    ]
    return base + fill


def _scored(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Valid, scored results: no skips, no errors, no memory 'off' control arms."""
    return [
        r
        for r in runs
        if "skipped" not in r
        and "error" not in r
        and not (r.get("kind") == "memory_ab" and r.get("arm") == "off")
    ]


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _rate(rows: list[dict[str, Any]], key: str = "passed") -> str:
    n, s = len(rows), sum(1 for r in rows if r.get(key))
    lo, hi = wilson(s, n)
    return f"{_pct(s / n if n else None)} ({s}/{n}; 95% CI {_pct(lo)}–{_pct(hi)})"


def _spend(runs: list[dict[str, Any]]) -> float:
    return float(
        sum(
            r.get("cost_usd", 0) + r.get("prime_cost_usd", 0) + r.get("judge_cost_usd", 0)
            for r in runs
            if "skipped" not in r
        )
    )


def headline() -> str:
    runs = baseline_runs()
    scored = _scored(runs)
    agentic = [r for r in scored if r.get("kind") in ("agentic", None)]
    reviewer = precision_recall(
        [(r["predicted_defect"], r["actual_defect"]) for r in scored if r.get("kind") == "reviewer"]
    )
    pairs: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    for r in runs:
        if r.get("kind") == "memory_ab" and "arm" in r and "error" not in r:
            pairs[(r["scenario"], r["rep"])][r["arm"]] = 1.0 if r.get("passed") else 0.0
    lift = paired_lift([(v["on"], v["off"]) for v in pairs.values() if {"on", "off"} <= set(v)])
    costs = [r["cost_usd"] for r in agentic if "cost_usd" in r]
    lats = [r["latency_s"] for r in agentic if r.get("latency_s")]
    loop_rows = [r for r in runs if "skipped" not in r and "error" not in r]
    lo, hi, mid = lift["low"], lift["high"], lift["lift"]
    lift_txt = (
        f"{100 * mid:+.1f} pts (n={lift['n']} pairs; bootstrap 95% CI "
        f"{100 * lo:+.1f} to {100 * hi:+.1f})"
        if mid is not None and lo is not None and hi is not None
        else "n/a"
    )
    rows = [
        ("Pass rate, all 60 scenarios (k=3)", _rate(scored)),
        ("Unsafe actions (must be 0)", f"**{sum(1 for r in runs if r.get('unsafe'))}**"),
        (
            "Reviewer precision / recall (planted defects)",
            f"{_pct(reviewer['precision'])} / {_pct(reviewer['recall'])} "
            f"(tp {reviewer['tp']}, fp {reviewer['fp']}, fn {reviewer['fn']}, tn {reviewer['tn']})",
        ),
        ("Escalation rate (agentic runs)", _rate(agentic, "escalated")),
        ("Loop rate (cap hit or no clean finish)", _rate(loop_rows, "loop")),
        ("Memory lift (pass rate, memory on − off)", lift_txt),
        (
            "Cost per agentic run, p50 / p95",
            f"${percentile(costs, 50):.4f} / ${percentile(costs, 95):.4f}" if costs else "n/a",
        ),
        (
            "Latency per agentic run, p50 / p95",
            f"{percentile(lats, 50):.1f} s / {percentile(lats, 95):.1f} s" if lats else "n/a",
        ),
        ("Eval spend (agents + judge)", f"${_spend(runs):.4f}"),
    ]
    out = [
        f"Baseline: first paid run `{BASELINE.name}` + `{FILL.name}` (the 3 runs its cost "
        "reservation skipped). Judge `claude-opus-5-5`. Wilson 95% intervals.",
        "",
        "| Metric | Baseline |",
        "|---|---|",
    ]
    out += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out)


def categories() -> str:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _scored(baseline_runs()):
        by[r.get("category", "?")].append(r)
    out = ["| Category | Scenarios | Baseline pass rate |", "|---|---|---|"]
    for cat, rows in sorted(by.items()):
        out.append(f"| {cat} | {len({r['scenario'] for r in rows})} | {_rate(rows)} |")
    return "\n".join(out)


def compare() -> str:
    base = _scored(baseline_runs())
    after_all = [r for r in _runs(AFTER) if "skipped" not in r]
    after = _scored(after_all)
    errored: dict[str, int] = defaultdict(int)
    for r in after_all:
        if "error" in r:
            errored[r["scenario"]] += 1
    ids = sorted({r["scenario"] for r in after_all})
    out = [
        f"After the fixes: `{AFTER.name}` (k=3 on the {len(ids)} SQL and email scenarios the "
        "fixes target). Errored runs hit the Anthropic workspace usage limit; they are not "
        "model results and are excluded.",
        "",
        "| Scenario | Baseline | After fixes (valid runs) | Errored |",
        "|---|---|---|---|",
    ]
    for sid in ids:
        b = [r for r in base if r["scenario"] == sid]
        a = [r for r in after if r["scenario"] == sid]
        bt = f"{sum(1 for r in b if r.get('passed'))}/{len(b)}"
        at = f"{sum(1 for r in a if r.get('passed'))}/{len(a)}" if a else "—"
        out.append(f"| {sid} | {bt} | {at} | {errored.get(sid, 0)} |")
    measured = {r["scenario"] for r in after}
    b_same = [r for r in base if r["scenario"] in measured]
    out += [
        "",
        f"Scenarios with at least one valid after-fix run ({len(measured)}): baseline "
        f"{_rate(b_same)} → after fixes {_rate(after)}. Spend `${_spend(after_all):.4f}`.",
    ]
    return "\n".join(out)


RENDER = {"headline": headline, "categories": categories, "compare": compare}


def _block(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(<!-- eval-table:{name} -->\n)(.*?)(\n<!-- /eval-table:{name} -->)", re.DOTALL
    )


def stale() -> list[str]:
    bad = []
    for path, names in TARGETS.items():
        text = path.read_text() if path.exists() else ""
        for name in names:
            m = _block(name).search(text)
            if m is None or m.group(2) != RENDER[name]():
                bad.append(f"{path.relative_to(ROOT)}:{name}")
    return bad


def write() -> list[str]:
    changed = []
    for path, names in TARGETS.items():
        text = path.read_text()
        new = text
        for name in names:
            body = RENDER[name]()

            def put(m: re.Match[str], body: str = body) -> str:
                return m.group(1) + body + m.group(3)

            new, n = _block(name).subn(put, new)
            if n != 1:
                raise ValueError(f"{path}: expected one '{name}' block, found {n}")
        if new != text:
            path.write_text(new)
            changed.append(str(path.relative_to(ROOT)))
    return changed
