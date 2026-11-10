# Security

AgentOps lets language models choose actions. The design assumes a model can be fooled by
what it reads, and makes sure a fooled model still can't do damage on its own. Details and
reasoning are in `docs/DECISIONS.md` (ADR numbers below).

## Threat model

| Threat | Main defences | Tested by |
|---|---|---|
| Prompt injection in web pages, files, MCP notes, SQL rows | Tool results framed as untrusted data (ADR-034); outbound actions are **proposals** a human approves (ADR-003); `fetch_url` only fetches URLs the task has already seen, so data can't leave through a URL (ADR-044) | `tests/integration/test_injection_vectors.py` (5 vectors, models scripted to obey the injection); eval injection category with real models: **0 unsafe actions in 180 runs** (`docs/EVALUATION.md`) |
| Memory poisoning (an injection that becomes a standing order) | Consolidation drops copied tool text, email addresses, standing directives and instruction phrasing; memories are recalled as labelled data, never instructions (ADR-038, 045) | injection suite (planted memory), memory unit tests |
| Executing or repeating an external action | Only `execute_actions` performs side effects, after approval, keyed in `side_effects`; the key also goes to the receiver (email `Message-ID`); a crash leaves "unknown", never a retry (ADR-004, 037) | SIGKILL tests at every step of the send path; eval crash category |
| Generated code | Docker sandbox: no network, read-only root, non-root, all capabilities dropped, memory/CPU/PID limits, timeout, empty environment; the worker reaches Docker only through a socket proxy that allows container create/start/wait/logs/remove and refuses exec, images, networks and volumes (ADR-011, 031) | `make test-sandbox` against a real daemon (own CI job) |
| SQL tool abuse | Separate read-only LOGIN role on the `demo` schema only, statement timeouts, a parser that allows exactly one SELECT with no data-modifying node (ADR-033) | tests that bypass the parser to prove the database alone refuses writes and role escape |
| Cross-user access | Every query is owner-scoped; another user's task, approval or memory is a 404; reviewers alone decide approvals (ADR-012) | API tests per route; a test walks every route and fails if one lacks auth |
| Token theft | Tokens are random 256-bit, stored as HMAC-SHA256 with a server pepper; the prod settings refuse the dev pepper (ADR-018); the browser's SSE stream uses single-use 60-second tickets, never the token in a URL (ADR-041); the UI keeps the token in `sessionStorage` only | auth and ticket tests (incl. concurrent redemption) |
| Secrets leaking into traces | Settings use `SecretStr`; trace payloads pass a redactor that masks secret-named keys and credential-shaped strings (ADR-042) | a test seeds secrets and asserts none come back |
| Browser attacks | CORS limited to the UI origin; CSP `default-src 'self'`, `script-src 'self'`, `frame-ancestors 'none'`, nosniff, no-referrer; model output rendered as sanitized markdown, never raw HTML (ADR-042) | lint rule bans `dangerouslySetInnerHTML`; e2e checks |
| Abuse / runaway cost | Per-user API rate limits (429 + `Retry-After`); per-tool token buckets; per-task budget ledger that refuses a model call before it could overspend (ADR-024) | rate-limit and ledger tests |
| Deleting audit records | `DELETE /tasks` and `make reset-demo` refuse when `ENV=prod`; reset asks for confirmation (ADR-050) | tests; verified on the booted prod profile |

## Supply chain

- Every Python and npm dependency is pinned exactly (lockfiles committed); npm installs run
  with `ignore-scripts`. New packages are vetted (publisher, published artifact, install hooks,
  network and file access) before adoption (ADR-016).
- CI runs `pip-audit` (Python) and `npm audit` (UI) on every push, and gitleaks over the full
  git history. Phase 6 security pass: pip-audit 2.10.1 and npm audit reported no known
  vulnerabilities; gitleaks v8.30.1 found 3 false positives (redactor test fixtures and the
  detect-secrets baseline), each allow-listed with its reason in `.gitleaksignore`.
- The embedding model is baked into the image at build time; at runtime the containers are
  offline for models (`HF_HUB_OFFLINE=1`).

## Known gaps

- Injection resistance is measured on 8 scenarios across 5 vectors, not by an adversarial red team.
- A fooled agent can still **propose** a harmful action; the defence is the human approving
  it. The approval UI shows the full arguments and lets the reviewer edit or reject.
- The memory screen is heuristic: it catches copied text and instruction phrasing, not every paraphrase.
- No TLS termination in the compose files: put a reverse proxy in front (`docs/DEPLOYMENT.md`).
- Auth is seeded bearer tokens with two roles; there is no OAuth/SSO, no token expiry and no admin UI.

## Reporting

This is a portfolio project. Please don't put exploit details in a public issue: open an issue
asking for a private contact, and details will be exchanged there.
