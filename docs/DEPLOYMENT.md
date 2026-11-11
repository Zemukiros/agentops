# Deployment

Two compose configurations share one image:

| | Dev (`docker-compose.yml`) | Prod profile (`+ docker-compose.prod.yml`) |
|---|---|---|
| `ENV` | `dev` | `prod` |
| Secrets | dev defaults | required: compose refuses to start without them |
| Email | Mailpit captures everything; nothing leaves | your SMTP relay, or **disabled** (proposals refused with the reason) |
| Postgres host port | `127.0.0.1:5433` | none |
| `DELETE /tasks`, `make reset-demo` | allowed (reset asks to type `reset`) | refused: audit records are never deleted |
| Env file | `.env` | `.env.prod` (the dev `.env` is never read) |

## Setup

### Linux, or Windows with WSL2

Developed and tested on **Docker Engine inside WSL2 (Ubuntu)**, without Docker Desktop.
Keep the repo in the WSL filesystem (`~/…`), not `/mnt/c`: the worker bind-mounts task
workspaces into sandbox containers by host path.

```bash
# prerequisites: Docker Engine + compose plugin, uv (Python 3.12), Node 24 LTS (UI dev only; CI uses 24.19.0)
git clone https://github.com/Zemukiros/agentops && cd agentops
cp .env.example .env
uv sync
make build-sandbox   # the code-execution image (never a service)
make up              # postgres, mailpit, socket proxy, mcp-notes, api, worker, ui
make migrate         # schema + checkpointer + seed users; tokens -> .agentops-tokens (0600)
```
Open http://localhost:5173 and sign in with `demo-user` or `demo-reviewer` from `.agentops-tokens`.

### macOS

The same commands work with Docker Desktop or Colima, with two caveats. This setup is
**not tested** on macOS. Bind mounts must be shared with the VM (the repo under your home
directory usually is). The socket proxy mounts `/var/run/docker.sock`; with Colima, point it
at the Colima socket or symlink it.

### Real models

Fake mode (scripted models) is the default and costs nothing. For real models set in `.env`:
`LLM_MODE=real`, `ANTHROPIC_API_KEY`, and the role models (commented block in `.env.example`).
Every task has a budget (`BUDGET__MAX_USD`, default $1.00), enforced before each model call.

## Production profile

```bash
cp .env.prod.example .env.prod          # untracked; fill in the three secrets at least
python -c "import secrets; print(secrets.token_urlsafe(32))"   # one per secret
make prod-config                        # resolved config (check it)
make prod-up                            # build + start
# seed users with tokens you generate and store yourself (nothing is written to the image):
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml \
  exec -e SEED_USER_TOKEN=... -e SEED_REVIEWER_TOKEN=... api python -m agentops.db.seed
```

**Email.** Set `SMTP_HOST` (+ `SMTP_PORT`, `SMTP_STARTTLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_FROM`) to a relay. Leave `SMTP_HOST` empty to disable email: the writing agent's
`send_email` is then refused with "email is disabled in this deployment: no SMTP relay is
configured", visible in the trace and the deliverable. An email approved after the relay was
removed is rejected with the same reason. Nothing is silently dropped.

**Verified.** The profile was booted as a separate compose project: migrations, API health,
an email task (refused with the reason), `DELETE` → 403, `reset-demo` refused, no database
port, no Mailpit (ADR-050).

**You still need** (not in the compose files):
- TLS termination and a public hostname: a reverse proxy (Caddy, nginx, Traefik) in front of
  the UI and API ports, which bind to `127.0.0.1` only. Set `UI_ORIGINS` to the public UI origin.
- Postgres backups (`pg_dump` or volume snapshots). All state, including the audit trail, is in Postgres.
- Log shipping and alerting. Optional traces: `OTEL_ENABLED=true` with the `observability` profile (Jaeger).
- A spend limit on the Anthropic workspace in addition to the per-task budgets.

## Static recorded demo (Vercel)

The public demo is the real UI built with `VITE_DEMO=1`, replaying one recorded real-model run
in the browser (ADR-053). No backend, no API key, $0 per visitor.

```bash
cd ui && npm run build:demo && npx vite preview   # local check on :5173
npx playwright test -c playwright.demo.config.ts  # click-through test of the demo build
```

Vercel (Hobby, $0): Add New → Project → import the repo, then set
- **Root Directory:** `ui`
- **Framework Preset:** Vite (the rest comes from `ui/vercel.json`: `npm ci`, `npm run build:demo`, output `dist`)
- **Environment variables:** none

`ui/vercel.json` adds the SPA rewrite and the same security headers as `ui/nginx.conf.template`,
with `connect-src 'self'` (the demo never calls an API).

To record a new run: start the stack in real mode, run `ui/e2e/real-walkthrough.spec.ts`
(`REAL_UI_WALKTHROUGH=1`, about $0.09 under a $0.20 cap; `REAL_UI_SHOTS` redirects its
screenshots), then `uv run python scripts/export_demo.py <task_id>` and review the diff of
`ui/src/demo/recording.json` before committing.

## Scaling

Workers are stateless: run more (`docker compose up -d --scale worker=3`). Jobs are claimed
with `SKIP LOCKED` under leases, and a task's segments are serialized by an advisory lock.
The API is stateless too. Postgres is the single point of state.

## Operations

| Task | Command |
|---|---|
| Status / logs | `make ps`, `make logs` |
| Wipe demo data (dev only) | `make reset-demo` (asks to type `reset`; keeps users) |
| Traces | `OTEL_ENABLED=true docker compose --profile observability up -d`, Jaeger on :16686 |
| Rotate demo tokens (dev) | `uv run python -m agentops.db.seed --rotate` |
