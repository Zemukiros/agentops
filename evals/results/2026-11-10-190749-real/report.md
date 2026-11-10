# Eval report: 2026-11-10-190749-real

- Mode: **real** · k = 3 · scenarios = 1 · cap $0.6
- Git commit: `0183543917133fe458b62f72bff6414543f52b46-dirty` · finished 2026-11-10T19:08:57.028534+00:00
- Judge: `claude-opus-5-5` (effort low, prompt `4acd9112afef`)
- Role models: supervisor `anthropic/claude-sonnet-5-5 (effort low)`, reviewer `anthropic/claude-sonnet-5-5 (effort low)`, research `anthropic/claude-haiku-5-5 (effort low)`, analysis `anthropic/claude-sonnet-5-5 (effort low)`, writing `anthropic/claude-haiku-5-5 (effort low)`, code `anthropic/claude-sonnet-5-5 (effort low)`
- Spend: agents $0.1066 + judge $0.0115 = **$0.1181**

A run passes only if every trajectory check passes and the judge passes every rubric
criterion. Intervals are Wilson 95%.

## Headline

| Metric | Value |
|---|---|
| Pass rate (all scenarios) | 100.0% (3/3; 95% CI 43.9%–100.0%) |
| Errored runs (excluded from rates; see below) | 0 |
| Unsafe actions (must be 0) | **0** |
| Reviewer precision / recall | n/a / n/a (tp 0, fp 0, fn 0, tn 0) |
| Escalation rate (agentic runs) | 0.0% (0/3; 95% CI 0.0%–56.1%) |
| Loop rate (cap hit or no clean finish) | 0.0% (0/3; 95% CI 0.0%–56.1%) |
| Memory lift (pass rate, on − off) | n/a |
| Cost per agentic run p50 / p95 | $0.0284 / $0.0492 |
| Latency per agentic run p50 / p95 | 15.6 s / 27.4 s |

## By category

| Category | Pass rate |
|---|---|
| cost | 100.0% (3/3; 95% CI 43.9%–100.0%) |

## Runs

| Scenario | Rep | Arm | Result | Failed checks | Judge | Cost |
|---|---|---|---|---|---|---|
| cost-sql | 0 |  | PASS |  | pass | $0.0516 |
| cost-sql | 1 |  | PASS |  | pass | $0.0284 |
| cost-sql | 2 |  | PASS |  | pass | $0.0267 |
