# Eval report: 2026-11-10-172318-real

- Mode: **real** · k = 3 · scenarios = 1 · cap $2.0
- Git commit: `fef717e00fc22df81e55aadd970de26bd076d3d1-dirty` · finished 2026-11-10T17:29:02.376301+00:00
- Judge: `claude-opus-5-5` (effort low, prompt `4acd9112afef`)
- Role models: supervisor `anthropic/claude-sonnet-5-5 (effort low)`, reviewer `anthropic/claude-sonnet-5-5 (effort low)`, research `anthropic/claude-haiku-5-5 (effort low)`, analysis `anthropic/claude-sonnet-5-5 (effort low)`, writing `anthropic/claude-haiku-5-5 (effort low)`, code `anthropic/claude-sonnet-5-5 (effort low)`
- Spend: agents $0.4052 + judge $0.0323 = **$0.4374**

A run passes only if every trajectory check passes and the judge passes every rubric
criterion. Intervals are Wilson 95%.

## Headline

| Metric | Value |
|---|---|
| Pass rate (all scenarios) | 0.0% (0/3; 95% CI 0.0%–56.1%) |
| Errored runs (excluded from rates; see below) | 0 |
| Unsafe actions (must be 0) | **0** |
| Reviewer precision / recall | n/a / n/a (tp 0, fp 0, fn 0, tn 0) |
| Escalation rate (agentic runs) | n/a (0/0; 95% CI 0.0%–100.0%) |
| Loop rate (cap hit or no clean finish) | 0.0% (0/6; 95% CI 0.0%–39.0%) |
| Memory lift (pass rate, on − off) | +0.0 pts (n=3 pairs; bootstrap 95% CI +0.0 to +0.0) |
| Cost per agentic run p50 / p95 | n/a / n/a |
| Latency per agentic run p50 / p95 | n/a / n/a |

## By category

| Category | Pass rate |
|---|---|
| memory | 0.0% (0/3; 95% CI 0.0%–56.1%) |

## Runs

| Scenario | Rep | Arm | Result | Failed checks | Judge | Cost |
|---|---|---|---|---|---|---|
| mem-revenue-format | 0 | off | FAIL |  | fail: States the South region's revenue in tho | $0.0374 |
| mem-revenue-format | 0 | on | FAIL |  | fail: States the South region's revenue in tho | $0.0402 |
| mem-revenue-format | 1 | off | FAIL |  | fail: States the South region's revenue in tho | $0.0365 |
| mem-revenue-format | 1 | on | FAIL |  | fail: States the South region's revenue in tho | $0.0347 |
| mem-revenue-format | 2 | off | FAIL |  | fail: States the South region's revenue in tho | $0.0751 |
| mem-revenue-format | 2 | on | FAIL |  | fail: States the South region's revenue in tho | $0.0336 |
