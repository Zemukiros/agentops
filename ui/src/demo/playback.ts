// Pure playback logic for the recorded demo (ADR-053). Given the recording of one real run and
// a playhead (how many of its events have been revealed), answer every API call the UI makes
// with what the real API would have returned at that moment. Nothing here is invented: every
// value comes from the recording; in-progress views hide what hadn't happened yet.
import type {
  Approval,
  ApprovalSummary,
  Budget,
  CostBucket,
  CostSummary,
  Me,
  TaskDetail,
  TaskEvent,
  TaskListItem,
  TaskStatus,
  TraceItem,
} from "../api/types";
import type { Memory } from "../pages/Memory";

export interface Recording {
  source: { task_id: string; note: string };
  identities: Me[];
  task: TaskListItem;
  detail: TaskDetail;
  events: TaskEvent[];
  trace: TraceItem[];
  cost: CostSummary;
  approvals: Approval[];
  memories: Memory[];
}

/** Playback pacing: real gaps replayed this much faster, each gap capped. */
export const SPEED = 1.6;
export const MAX_GAP_MS = 3500;

const ms = (iso: string): number => new Date(iso).getTime();

/** Delay before each event is revealed; 0 for the first. */
export function delays(events: TaskEvent[], speed = SPEED, cap = MAX_GAP_MS): number[] {
  return events.map((e, i) => (i === 0 ? 0 : Math.min(cap, Math.max(0, ms(e.created_at) - ms(events[i - 1]!.created_at)) / speed)));
}

/** The approval a visitor must decide before event `i` can be revealed, if any. */
export function gateAt(events: TaskEvent[], i: number): string | null {
  const e = events[i];
  return e?.type === "approval.decided" ? String(e.payload.approval_id) : null;
}

/** Shift every recorded timestamp so the run appears to start at `startMs`. */
export function shifter(rec: Recording, startMs: number): (iso: string | null) => string | null {
  const offset = startMs - ms(rec.events[0]!.created_at);
  return (iso) => (iso ? new Date(ms(iso) + offset).toISOString() : iso);
}

export interface View {
  rec: Recording;
  shown: TaskEvent[];
  shift: (iso: string | null) => string | null;
}

export function view(rec: Recording, revealed: number, startMs: number): View {
  return { rec, shown: rec.events.slice(0, revealed), shift: shifter(rec, startMs) };
}

const has = (v: View, type: string, pred: (p: Record<string, unknown>) => boolean = () => true): boolean =>
  v.shown.some((e) => e.type === type && pred(e.payload));

const lastId = (v: View): number => v.shown.at(-1)?.id ?? 0;
const finished = (v: View): boolean => has(v, "task.finished");

export function status(v: View): TaskStatus {
  for (let i = v.shown.length - 1; i >= 0; i--) {
    const e = v.shown[i]!;
    if (e.type === "task.finished" || e.type === "task.status") return e.payload.status as TaskStatus;
  }
  return "queued";
}

function decided(v: View, approvalId: string): boolean {
  return has(v, "approval.decided", (p) => p.approval_id === approvalId);
}

function requested(v: View, approvalId: string): boolean {
  return has(v, "approval.requested", (p) => p.approval_id === approvalId);
}

function llmCalls(v: View): Record<string, unknown>[] {
  return v.shown.filter((e) => e.type === "llm.call").map((e) => e.payload);
}

export function budget(v: View): Budget | null {
  const b = v.rec.detail.budget;
  if (!b) return null;
  if (finished(v)) return b;
  const calls = llmCalls(v);
  const sum = (k: string) => calls.reduce((n, c) => n + Number(c[k] ?? 0), 0);
  return { ...b, used_usd: sum("cost_usd"), used_tokens: sum("input_tokens") + sum("output_tokens"), used_calls: calls.length };
}

function approvalSummary(v: View, a: ApprovalSummary): ApprovalSummary {
  const done = decided(v, a.id);
  return {
    ...a,
    status: done ? a.status : "pending",
    version: done ? a.version : Math.max(1, a.version - 1),
    created_at: v.shift(a.created_at)!,
    decided_at: done ? v.shift(a.decided_at) : null,
    expires_at: v.shift(a.expires_at)!,
  };
}

export function approval(v: View, a: Approval): Approval {
  const done = decided(v, a.id);
  return {
    ...a,
    ...approvalSummary(v, a),
    decision: done ? a.decision : null,
    decided_by: done ? a.decided_by : null,
  };
}

export function approvals(v: View, state: "pending" | "decided" | "all" = "all"): Approval[] {
  return v.rec.approvals
    .filter((a) => requested(v, a.id))
    .map((a) => approval(v, a))
    .filter((a) => state === "all" || (state === "pending" ? a.status === "pending" : a.status !== "pending"))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export function detail(v: View): TaskDetail {
  const d = v.rec.detail;
  const done = finished(v);
  const last = v.shown.at(-1);
  const subtasks = d.subtasks.map((s) => {
    const fin = v.shown.findLast((e) => e.type === "subtask.finished" && e.payload.subtask_id === s.id);
    const resolved = has(v, "escalation.resolved", (p) => (p.subtask_ids as string[] | undefined)?.includes(s.id) ?? false);
    const reviews = s.reviews.slice(0, v.shown.filter((e) => e.type === "review.verdict" && e.payload.subtask_id === s.id).length);
    if (resolved || done) return s;
    if (fin)
      return { ...s, status: String(fin.payload.status), revisions: Number(fin.payload.revisions), reason: (fin.payload.reason as string | null) ?? null, human_override: false, reviews };
    const dispatched = has(v, "subtask.dispatched", (p) => p.subtask_id === s.id);
    // revisions made so far: one per "revise" verdict, never more than the run made
    const revisions = Math.min(s.revisions, reviews.filter((r) => r.verdict === "revise").length);
    return { ...s, status: dispatched ? "running" : "pending", revisions, output: null, reason: null, human_override: false, reviews };
  });
  const proposed = (hash: string) => has(v, "tool.call", (p) => p.proposal_hash === hash && p.status === "proposed");
  const actions = d.actions
    .filter((a) => proposed(a.hash))
    .map((a) => {
      const decidedNow = has(v, "action.decided", (p) => p.hash === a.hash);
      const outcome = has(v, "action.outcome", (p) => p.hash === a.hash);
      const appr = a.approval && requested(v, a.approval.id) ? { ...a.approval, ...(decided(v, a.approval.id) ? {} : { status: "pending", decision: null, version: Math.max(1, a.approval.version - 1) }) } : null;
      return { ...a, approval: appr, decision: decidedNow ? a.decision : null, outcome: outcome ? a.outcome : null, side_effect_status: outcome ? a.side_effect_status : null };
    });
  const reviewed = has(v, "final_review.verdict");
  return {
    ...d,
    status: status(v),
    created_at: v.shift(d.created_at)!,
    updated_at: v.shift(last?.created_at ?? d.created_at)!,
    budget: budget(v),
    plan: has(v, "plan.created") ? d.plan : null,
    plan_confidence: has(v, "plan.confidence") ? d.plan_confidence : null,
    memories: has(v, "memory.recalled") ? d.memories : [],
    subtasks,
    actions,
    approvals: d.approvals.filter((a) => requested(v, a.id)).map((a) => approvalSummary(v, a)),
    final: reviewed ? d.final : null,
    final_review: reviewed ? d.final_review : null,
    outcome: done ? d.outcome : null,
    explanation: done ? d.explanation : null,
    failure_reason: done ? d.failure_reason : null,
  };
}

export function listItem(v: View): TaskListItem {
  const t = v.rec.task;
  const b = budget(v);
  const first = v.rec.events[0]!;
  const last = v.shown.at(-1) ?? first;
  return {
    ...t,
    status: status(v),
    cost_usd: b?.used_usd ?? t.cost_usd,
    llm_calls: b?.used_calls ?? t.llm_calls,
    duration_s: finished(v) ? t.duration_s : (ms(last.created_at) - ms(first.created_at)) / 1000,
    pending_approvals: approvals(v, "pending").length,
    created_at: v.shift(t.created_at)!,
    updated_at: v.shift(last.created_at)!,
  };
}

export function events(v: View): TaskEvent[] {
  return v.shown.map((e) => ({ ...e, created_at: v.shift(e.created_at)! }));
}

export function trace(v: View): TraceItem[] {
  const upTo = lastId(v);
  return v.rec.trace.filter((t) => t.id <= upTo).map((t) => ({ ...t, at: v.shift(t.at)! }));
}

function bucket(): CostBucket {
  return { calls: 0, errors: 0, retries: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0, latency_ms: 0 };
}

export function cost(v: View): CostSummary {
  if (finished(v)) return v.rec.cost;
  const total = bucket();
  const by_agent: Record<string, CostBucket> = {};
  const by_model: Record<string, CostBucket> = {};
  for (const c of llmCalls(v)) {
    const model = `anthropic/${String(c.model)}`;
    for (const b of [total, (by_agent[String(c.role)] ??= bucket()), (by_model[model] ??= bucket())]) {
      b.calls += 1;
      b.errors += c.ok ? 0 : 1;
      b.input_tokens += Number(c.input_tokens ?? 0);
      b.output_tokens += Number(c.output_tokens ?? 0);
      b.cost_usd += Number(c.cost_usd ?? 0);
      b.latency_ms += Number(c.latency_ms ?? 0);
    }
  }
  const tools: CostSummary["tools"] = {};
  for (const e of v.shown.filter((e) => e.type === "tool.call")) {
    const t = (tools[String(e.payload.tool)] ??= { calls: 0, errors: 0, latency_ms: 0 });
    t.calls += 1;
    t.errors += ["error", "denied"].includes(String(e.payload.status)) ? 1 : 0; // as views.cost_summary
    t.latency_ms += Number(e.payload.latency_ms ?? 0);
  }
  const first = v.rec.events[0]!;
  const last = v.shown.at(-1) ?? first;
  const wall = (ms(last.created_at) - ms(first.created_at)) / 1000;
  const waits = v.rec.approvals
    .filter((a) => decided(v, a.id) && a.decided_at)
    .map((a) => ({ a, wait: (ms(a.decided_at!) - ms(a.created_at)) / 1000 }));
  const human = waits.reduce((n, w) => n + w.wait, 0);
  const queue = Math.min(v.rec.cost.time.queue_s, wall); // the recorded queue time (claiming the job)
  return {
    task_id: v.rec.task.id,
    total,
    by_agent,
    by_model,
    tools,
    budget: budget(v),
    time: { wall_s: wall, compute_s: Math.max(0, wall - human - queue), human_wait_s: human, queue_s: queue },
    approvals: approvals(v).map((a) => ({
      kind: a.kind,
      key: a.key,
      status: a.status,
      blocking: a.blocking,
      wait_s: waits.find((w) => w.a.id === a.id)?.wait ?? 0,
    })),
  };
}

/** The Memory page: only memories this run wrote, and only once it has written them. */
export function memories(v: View): Memory[] {
  if (!has(v, "memory.consolidated")) return [];
  return v.rec.memories
    .filter((m) => m.source_task_id === v.rec.task.id)
    .map((m) => ({ ...m, created_at: v.shift(m.created_at)! }));
}
