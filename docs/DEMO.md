# Demo walkthrough

About 5 minutes, $0: fake mode replays scripted models through the real system (API, queue,
worker, graph, tools, Mailpit). Every screen shows real data from that run.

## Setup

```bash
make up && make migrate              # see docs/DEPLOYMENT.md for prerequisites
FAKE_LATENCY_MS=600 make up          # optional: pace the run so you can watch it unfold
```
Open http://localhost:5173 and sign in with the `demo-user` token from `.agentops-tokens`.
Approvals need the reviewer: sign in as `demo-reviewer` in a second (private) window.

## Script

1. **New task.** Keep the default scenario `ui_demo` (research, a brief revised once, an email
   that waits for approval). Request: *"Brief the team on pgvector and email it to team@example.com."*
2. **Watch the plan run.** The task page shows the plan DAG live over SSE: research, then the
   brief. The reviewer sends the brief back once ("revise"), and the second draft is accepted.
   Each node shows its attempts, reviews and tool calls.
3. **Approve the email.** The task stops at `awaiting_approval`. In the reviewer window, open
   **Approvals**: the email's arguments appear as a form generated from the tool's schema.
   Approve it as is, or edit the subject first.
4. **See it delivered once.** Back on the task: `completed`, the deliverable, and the email
   with a link to it in Mailpit (http://localhost:8025). The send was keyed by an idempotency
   key, which is also the email's `Message-ID`.
5. **Trace and cost.** The trace tab: every model and tool call in order, compute time vs.
   time spent waiting for a human, and cost per agent and per model (fake models are priced at $0).
6. **Replay.** Fork the task at a checkpoint, optionally editing what that point allows (the
   request, the plan, an accepted subtask output, or the final text), run the branch, and
   compare the two branches side by side. A branch never re-sends the email.
7. **Memory.** The memory page lists what the run learned (an episode and lessons), with
   search, and a delete button per memory.

## Other scenarios worth showing

| Scenario | Shows |
|---|---|
| `low_confidence_plan` | the plan gate: a human approves or edits the plan before any work |
| `revisions_exhausted` | the escalation gate after 2 rejected revisions: approve, edit, reject or take over |
| `partial_ok` | a failed subtask; the rest is delivered with a "What's missing" section |
| `specialist_error` | provider failures exhausting retries; the task fails cleanly |

## Crash recovery (terminal)

```bash
FAKE_LATENCY_MS=1500 make up
# start a ui_demo task in the UI, then while it runs:
docker compose kill worker && docker compose up -d worker
```
The task continues from its last checkpoint on the restarted worker; the trace shows a new
run segment with `resumed_from_checkpoint: true`, and the email is still sent at most once.

## Reset

`make reset-demo` (dev only; type `reset` to confirm) clears tasks, memories and Mailpit and
keeps the users.
