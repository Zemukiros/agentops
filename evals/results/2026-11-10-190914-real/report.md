# Eval report: 2026-11-10-190914-real

- Mode: **real** · k = 1 · scenarios = 6 · cap $2.3
- Git commit: `0183543917133fe458b62f72bff6414543f52b46-dirty` · finished 2026-11-10T19:21:27.598387+00:00
- Judge: `claude-opus-5-5` (effort low, prompt `4acd9112afef`)
- Role models: supervisor `anthropic/claude-sonnet-5-5 (effort low)`, reviewer `anthropic/claude-sonnet-5-5 (effort low)`, research `anthropic/claude-haiku-5-5 (effort low)`, analysis `anthropic/claude-sonnet-5-5 (effort low)`, writing `anthropic/claude-haiku-5-5 (effort low)`, code `anthropic/claude-sonnet-5-5 (effort low)`
- Spend: agents $1.0212 + judge $0.0939 = **$1.1151**

A run passes only if every trajectory check passes and the judge passes every rubric
criterion. Intervals are Wilson 95%.

## Headline

| Metric | Value |
|---|---|
| Pass rate (all scenarios) | 80.0% (4/5; 95% CI 37.6%–96.4%) |
| Errored runs (excluded from rates; see below) | 0 |
| Unsafe actions (must be 0) | **0** |
| Reviewer precision / recall | n/a / n/a (tp 0, fp 0, fn 0, tn 0) |
| Escalation rate (agentic runs) | n/a (0/0; 95% CI 0.0%–100.0%) |
| Loop rate (cap hit or no clean finish) | 0.0% (0/10; 95% CI 0.0%–27.8%) |
| Memory lift (pass rate, on − off) | +80.0 pts (n=5 pairs; bootstrap 95% CI +40.0 to +100.0) |
| Cost per agentic run p50 / p95 | n/a / n/a |
| Latency per agentic run p50 / p95 | n/a / n/a |

## By category

| Category | Pass rate |
|---|---|
| memory | 80.0% (4/5; 95% CI 37.6%–96.4%) |

## Runs

| Scenario | Rep | Arm | Result | Failed checks | Judge | Cost |
|---|---|---|---|---|---|---|
| mem-no-jargon | 0 | off | FAIL |  | fail: Is understandable by a non-technical exe | $0.0469 |
| mem-no-jargon | 0 | on | FAIL |  | fail: Is understandable by a non-technical exe | $0.0338 |
| mem-recommendation-first | 0 | off | FAIL |  | fail: The first line has the form "Recommendat | $0.2020 |
| mem-recommendation-first | 0 | on | PASS |  | pass | $0.1452 |
| mem-revenue-format | 0 | | skipped | cap $2.30: spent $1.1151, in flight $0.0000, next run needs  | | |
| mem-sources-line | 0 | off | FAIL |  | fail: The brief ends with a line that starts w | $0.0673 |
| mem-sources-line | 0 | on | PASS |  | pass | $0.0967 |
| mem-tldr-first | 0 | off | FAIL |  | fail: The summary starts with a one-line TL;DR | $0.0246 |
| mem-tldr-first | 0 | on | PASS |  | pass | $0.0264 |
| mem-word-limit | 0 | off | FAIL |  | fail: The summary is at most 80 words. | $0.0387 |
| mem-word-limit | 0 | on | PASS |  | pass | $0.0175 |

## Errors and skipped runs

- skipped mem-revenue-format#0: cap $2.30: spent $1.1151, in flight $0.0000, next run needs up to $1.3680
