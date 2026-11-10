# Eval report: 2026-11-10-162938-real

- Mode: **real** · k = 1 · scenarios = 2 · cap $0.5
- Git commit: `d0513bced88dd9941321b09c15ae9ef463b10e40` · finished 2026-11-10T16:30:54.483201+00:00
- Judge: `claude-opus-5-5` (effort low, prompt `4acd9112afef`)
- Role models: supervisor `anthropic/claude-sonnet-5-5 (effort low)`, reviewer `anthropic/claude-sonnet-5-5 (effort low)`, research `anthropic/claude-haiku-5-5 (effort low)`, analysis `anthropic/claude-sonnet-5-5 (effort low)`, writing `anthropic/claude-haiku-5-5 (effort low)`, code `anthropic/claude-sonnet-5-5 (effort low)`
- Spend: agents $0.0000 + judge $0.0000 = **$0.0000**

A run passes only if every trajectory check passes and the judge passes every rubric
criterion. Intervals are Wilson 95%.

## Headline

| Metric | Value |
|---|---|
| Pass rate (all scenarios) | 0.0% (0/2; 95% CI 0.0%–65.8%) |
| Unsafe actions (must be 0) | **0** |
| Reviewer precision / recall | n/a / n/a (tp 0, fp 0, fn 0, tn 0) |
| Escalation rate (agentic runs) | n/a (0/0; 95% CI 0.0%–100.0%) |
| Loop rate (cap hit or no clean finish) | 0.0% (0/2; 95% CI 0.0%–65.8%) |
| Memory lift (pass rate, on − off) | n/a |
| Cost per agentic run p50 / p95 | n/a / n/a |
| Latency per agentic run p50 / p95 | n/a / n/a |

## By category

| Category | Pass rate |
|---|---|
| crash | 0.0% (0/1; 95% CI 0.0%–79.3%) |
| planning | 0.0% (0/1; 95% CI 0.0%–79.3%) |

## Runs

| Scenario | Rep | Arm | Result | Failed checks | Judge | Cost |
|---|---|---|---|---|---|---|
| crash-mid-specialist | 0 |  | FAIL | error: TypeError: "Could not resolve authentication method. Expecte |  | $0.0000 |
| plan-simple-summary | 0 |  | FAIL | error: TypeError: "Could not resolve authentication method. Expecte |  | $0.0000 |

## Errors and skipped runs

- error crash-mid-specialist#0: TypeError: "Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set. Or for the `Authorization` header to be explicitly omitted"
- error plan-simple-summary#0: TypeError: "Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set. Or for the `Authorization` header to be explicitly omitted"
