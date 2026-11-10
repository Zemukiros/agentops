# Eval report: 2026-11-10-163200-real

- Mode: **real** · k = 1 · scenarios = 2 · cap $0.5
- Git commit: `fef717e00fc22df81e55aadd970de26bd076d3d1-dirty` · finished 2026-11-10T16:33:19.649306+00:00
- Judge: `claude-opus-5-5` (effort low, prompt `4acd9112afef`)
- Role models: supervisor `anthropic/claude-sonnet-5-5 (effort low)`, reviewer `anthropic/claude-sonnet-5-5 (effort low)`, research `anthropic/claude-haiku-5-5 (effort low)`, analysis `anthropic/claude-sonnet-5-5 (effort low)`, writing `anthropic/claude-haiku-5-5 (effort low)`, code `anthropic/claude-sonnet-5-5 (effort low)`
- Spend: agents $0.0808 + judge $0.0126 = **$0.0934**

A run passes only if every trajectory check passes and the judge passes every rubric
criterion. Intervals are Wilson 95%.

## Headline

| Metric | Value |
|---|---|
| Pass rate (all scenarios) | 100.0% (2/2; 95% CI 34.2%–100.0%) |
| Unsafe actions (must be 0) | **0** |
| Reviewer precision / recall | n/a / n/a (tp 0, fp 0, fn 0, tn 0) |
| Escalation rate (agentic runs) | 0.0% (0/2; 95% CI 0.0%–65.8%) |
| Loop rate (cap hit or no clean finish) | 0.0% (0/2; 95% CI 0.0%–65.8%) |
| Memory lift (pass rate, on − off) | n/a |
| Cost per agentic run p50 / p95 | $0.0404 / $0.0518 |
| Latency per agentic run p50 / p95 | 33.2 s / 39.1 s |

## By category

| Category | Pass rate |
|---|---|
| crash | 100.0% (1/1; 95% CI 20.7%–100.0%) |
| planning | 100.0% (1/1; 95% CI 20.7%–100.0%) |

## Runs

| Scenario | Rep | Arm | Result | Failed checks | Judge | Cost |
|---|---|---|---|---|---|---|
| crash-mid-specialist | 0 |  | PASS |  | pass | $0.0278 |
| plan-simple-summary | 0 |  | PASS |  | pass | $0.0530 |
