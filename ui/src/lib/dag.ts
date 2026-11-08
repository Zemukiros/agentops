// Pure logic behind the live plan DAG: layout and per-subtask status from the event stream.
import type { SubtaskView, TaskEvent } from "../api/types";

export interface PlanNode {
  id: string;
  description: string;
  specialist: string;
  depends_on: string[];
}

export type LiveStatus =
  | "pending"
  | "dispatched"
  | "working"
  | "in_review"
  | "revising"
  | "accepted"
  | "failed"
  | "needs_human"
  | "blocked";

export interface LiveSubtask {
  status: LiveStatus;
  revision: number;
  score: number | null;
  verdict: string | null;
  toolCalls: number;
  lastTool: string | null;
  wave: number | null;
}

const TERMINAL_STATUSES = new Set<LiveStatus>(["accepted", "failed", "needs_human", "blocked"]);

function blank(): LiveSubtask {
  return {
    status: "pending",
    revision: 0,
    score: null,
    verdict: null,
    toolCalls: 0,
    lastTool: null,
    wave: null,
  };
}

/** The newest plan in the stream (a re-plan or a human plan edit replaces it). */
export function planFromEvents(events: TaskEvent[]): PlanNode[] | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]!;
    if (e.type === "plan.created" || e.type === "plan_gate.resolved") {
      const plan = (e.payload.plan ?? null) as { subtasks?: PlanNode[] } | null;
      if (plan?.subtasks?.length) return plan.subtasks;
    }
  }
  return null;
}

/** Fold the event stream into per-subtask live status. Order-independent within a
 * subtask's terminal state: once finished, later noise can't move it back. */
export function liveSubtasks(events: TaskEvent[]): Record<string, LiveSubtask> {
  const out: Record<string, LiveSubtask> = {};
  const get = (id: string): LiveSubtask => (out[id] ??= blank());
  for (const e of events) {
    const p = e.payload ?? {};
    const sid = typeof p.subtask_id === "string" ? p.subtask_id : null;
    if (!sid) continue;
    const s = get(sid);
    const finished = TERMINAL_STATUSES.has(s.status);
    switch (e.type) {
      case "subtask.dispatched":
        // a dispatched subtask may be re-dispatched after a human escalation decision
        s.status = "dispatched";
        s.wave = typeof p.wave === "number" ? p.wave : s.wave;
        break;
      case "node.started":
        if (e.node === "specialist" && !finished) s.status = "working";
        break;
      case "node.completed":
        if (e.node === "specialist" && !finished) s.status = "in_review";
        break;
      case "tool.call":
        s.toolCalls += 1;
        s.lastTool = typeof p.tool === "string" ? p.tool : s.lastTool;
        break;
      case "review.verdict":
        s.score = typeof p.score === "number" ? p.score : s.score;
        s.verdict = typeof p.verdict === "string" ? p.verdict : s.verdict;
        s.revision = typeof p.revision === "number" ? p.revision : s.revision;
        if (!finished) s.status = p.verdict === "revise" ? "revising" : "in_review";
        break;
      case "subtask.blocked":
        s.status = "blocked";
        break;
      case "subtask.finished":
        s.status = (p.status as LiveStatus) ?? s.status;
        break;
    }
  }
  return out;
}

/** Merge the authoritative snapshot (task detail) with the live stream: a terminal status
 * from either source wins; otherwise the stream (newer) wins. */
export function mergeStatus(snapshot: SubtaskView | undefined, live: LiveSubtask | undefined): LiveStatus {
  const snap = (snapshot?.status ?? "pending") as LiveStatus;
  if (TERMINAL_STATUSES.has(snap)) return snap;
  if (live && live.status !== "pending") return live.status;
  return snap;
}

/** The newest task status reported by the stream (task.status / task.finished), if any. */
export function liveTaskStatus(events: TaskEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]!;
    if ((e.type === "task.status" || e.type === "task.finished") && typeof e.payload.status === "string")
      return e.payload.status;
  }
  return null;
}

/** Subtask rows: the snapshot's, plus placeholders for planned subtasks it doesn't have yet. */
export function subtaskRows(plan: PlanNode[], snapshot: SubtaskView[]): SubtaskView[] {
  const known = new Map(snapshot.map((s) => [s.id, s]));
  const rows = plan.map(
    (p) =>
      known.get(p.id) ?? {
        id: p.id,
        description: p.description,
        specialist: p.specialist,
        depends_on: p.depends_on,
        expected_output: "",
        complexity: "",
        status: "pending",
        revisions: 0,
        output: null,
        reason: null,
        human_override: false,
        reviews: [],
      },
  );
  const planned = new Set(plan.map((p) => p.id));
  return [...rows, ...snapshot.filter((s) => !planned.has(s.id))];
}

export interface Positioned {
  id: string;
  level: number;
  row: number;
}

/** Layered layout: level = longest dependency chain to the node; rows ordered by plan order.
 * Unknown dependencies are ignored; a cycle (never valid, but defensively) can't loop. */
export function layout(nodes: PlanNode[]): Positioned[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const memo = new Map<string, number>();
  const visiting = new Set<string>();
  const level = (id: string): number => {
    const cached = memo.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const deps = (byId.get(id)?.depends_on ?? []).filter((d) => byId.has(d));
    const l = deps.length ? Math.max(...deps.map(level)) + 1 : 0;
    visiting.delete(id);
    memo.set(id, l);
    return l;
  };
  const rows = new Map<number, number>();
  return nodes.map((n) => {
    const l = level(n.id);
    const r = rows.get(l) ?? 0;
    rows.set(l, r + 1);
    return { id: n.id, level: l, row: r };
  });
}
