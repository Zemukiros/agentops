"""Cost estimate shown (and confirmed) before any paid eval run.

Agent cost per task is modelled on the real runs recorded in PROGRESS.md (same role models,
effort low): $0.040 (1 subtask, 6 calls), $0.042 (2 subtasks), $0.076-0.078 (3 subtasks)
-> 0.025 + 0.017 x subtasks. Reviewer component: one Sonnet review call (measured $0.015;
estimated $0.02). Judge input tokens come from the free count_tokens endpoint on the real judge
prompt for each scenario (with a 700-word stand-in deliverable); judge output is budgeted at
1,000 tokens (effort low; JSON verdicts)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentops.models import pricing
from evals.judge import JUDGE_MODEL, SYSTEM, user_prompt
from evals.spec import Scenario

PER_TASK_BASE = 0.025
PER_SUBTASK = 0.017
REVIEWER_CALL = 0.02
JUDGE_OUTPUT_TOKENS = 1000
STAND_IN = ("Lorem ipsum dolor sit amet consectetur. " * 100).strip()  # ~700 words


@dataclass
class Line:
    scenario: str
    category: str
    runs: int
    agent_usd: float
    judge_runs: int
    judge_usd: float


def agent_cost(s: Scenario) -> float:
    if s.kind == "reviewer":
        return REVIEWER_CALL
    one = PER_TASK_BASE + PER_SUBTASK * s.expected_subtasks
    if s.kind == "memory_ab":
        return one * (len(s.prime) + 2)  # primes + arm on + arm off
    return one


async def judge_input_tokens(s: Scenario, client: Any | None) -> int:
    if not s.rubric:
        return 0
    prompt = user_prompt(s.request or "", s.rubric, STAND_IN, "Final status: completed\nPlan: ...")
    if client is None:  # offline: rough 4 chars per token
        return (len(SYSTEM) + len(prompt)) // 4
    r = await client.messages.count_tokens(
        model=JUDGE_MODEL, system=SYSTEM, messages=[{"role": "user", "content": prompt}]
    )
    return int(r.input_tokens)


async def estimate(
    scenarios: list[Scenario], k: int, client: Any | None = None, judge: str = JUDGE_MODEL
) -> list[Line]:
    price = pricing.lookup("anthropic", judge)
    assert price is not None and price.priced
    lines = []
    for s in scenarios:
        judged = (2 if s.kind == "memory_ab" else 1) * k if s.rubric else 0
        tin = await judge_input_tokens(s, client)
        assert price.input_per_mtok is not None and price.output_per_mtok is not None
        per_judgment = (
            tin * price.input_per_mtok + JUDGE_OUTPUT_TOKENS * price.output_per_mtok
        ) / 1e6
        lines.append(Line(s.id, s.category, k, agent_cost(s) * k, judged, per_judgment * judged))
    return lines


def render(lines: list[Line], k: int, cap: float, judge: str = JUDGE_MODEL) -> str:
    by_cat: dict[str, list[Line]] = {}
    for line in lines:
        by_cat.setdefault(line.category, []).append(line)
    out = [f"Eval estimate (k={k}, cap ${cap:.2f}, judge {judge})", ""]
    out.append(
        f"{'category':<12}{'scen':>5}{'runs':>6}{'agents $':>11}{'judged':>8}{'judge $':>10}"
    )
    for cat, ls in sorted(by_cat.items()):
        out.append(
            f"{cat:<12}{len(ls):>5}{sum(x.runs for x in ls):>6}"
            f"{sum(x.agent_usd for x in ls):>11.2f}"
            f"{sum(x.judge_runs for x in ls):>8}{sum(x.judge_usd for x in ls):>10.2f}"
        )
    a = sum(x.agent_usd for x in lines)
    j = sum(x.judge_usd for x in lines)
    out += [
        "",
        f"agents ${a:.2f} + judge ${j:.2f} = estimated total ${a + j:.2f} (hard cap ${cap:.2f})",
    ]
    return "\n".join(out)
