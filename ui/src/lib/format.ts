// Formatting helpers (pure).
import type { TaskEvent } from "../api/types";

export function usd(n: number | null | undefined, digits = 4): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (n === 0) return "$0";
  if (Math.abs(n) < 0.0001) return "<$0.0001";
  return `$${n.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "")}`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m} min ${s} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}

export function compact(n: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function relativeTime(iso: string, now: number = Date.now()): string {
  const diff = (now - new Date(iso).getTime()) / 1000;
  if (diff < -45) {
    const ahead = -diff;
    if (ahead < 3600) return `in ${Math.round(ahead / 60)} min`;
    if (ahead < 86400 * 2) return `in ${Math.round(ahead / 3600)} h`;
    return `in ${Math.round(ahead / 86400)} days`;
  }
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function humanize(s: string): string {
  return s.replace(/[_.]/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export type FeedTone = "info" | "success" | "warning" | "danger" | "neutral" | "violet";
export interface FeedLine {
  id: number;
  at: string;
  text: string;
  tone: FeedTone;
  /** model calls are numerous; the feed hides them unless asked */
  detail: boolean;
}

const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

/** Human-readable activity line for an event, or null for events not worth a line. */
export function feedLine(e: TaskEvent): FeedLine | null {
  const p = e.payload ?? {};
  const base = { id: e.id, at: e.created_at, detail: false };
  const sid = str(p.subtask_id);
  switch (e.type) {
    case "task.created":
      return { ...base, text: p.replay ? "Replay branch created" : "Task created", tone: "neutral" };
    case "run.segment_started":
      return {
        ...base,
        text: p.resumed_from_checkpoint ? "Worker resumed from checkpoint" : "Worker started",
        tone: "neutral",
      };
    case "replay.workspace_restored":
      return { ...base, text: `Workspace restored (${str(p.from)})`, tone: "violet" };
    case "memory.recalled": {
      const n = num(p.count) ?? 0;
      return { ...base, text: n ? `Recalled ${n} past example${n > 1 ? "s" : ""}` : "No similar past tasks", tone: "neutral" };
    }
    case "plan.created": {
      const n = ((p.plan as { subtasks?: unknown[] } | undefined)?.subtasks ?? []).length;
      return { ...base, text: `Plan created: ${n} subtask${n === 1 ? "" : "s"}`, tone: "info" };
    }
    case "plan.invalid":
      return { ...base, text: "Plan invalid, re-planning", tone: "warning" };
    case "plan.confidence":
      return {
        ...base,
        text: `Plan confidence ${(num(p.effective) ?? 0).toFixed(2)} (threshold ${(num(p.threshold) ?? 0).toFixed(2)})`,
        tone: p.needs_approval ? "warning" : "neutral",
        detail: !p.needs_approval,
      };
    case "subtask.dispatched":
      return { ...base, text: `${sid} dispatched (wave ${num(p.wave) ?? "?"})`, tone: "info" };
    case "node.started":
      if (e.node === "specialist") return { ...base, text: `${sid}: specialist working`, tone: "info" };
      if (e.node === "synthesize") return { ...base, text: "Synthesizing the deliverable", tone: "info" };
      return null;
    case "tool.call": {
      const st = str(p.status);
      return {
        ...base,
        text: `${str(p.agent)} → ${str(p.tool)} (${st})`,
        tone: st === "error" || st === "denied" || st === "disabled" ? "danger" : st === "proposed" ? "violet" : "neutral",
      };
    }
    case "llm.call":
      return {
        ...base,
        text: `${str(p.role)} · ${str(p.purpose)} · ${num(p.input_tokens) ?? 0}+${num(p.output_tokens) ?? 0} tok${p.ok === false ? " · failed" : ""}`,
        tone: p.ok === false ? "danger" : "neutral",
        detail: p.ok !== false,
      };
    case "review.verdict": {
      const v = str(p.verdict);
      return {
        ...base,
        text: `${sid}: review ${v} (${num(p.score) ?? "?"}/5)`,
        tone: v === "accept" ? "success" : v === "revise" ? "warning" : "danger",
      };
    }
    case "subtask.finished": {
      const st = str(p.status);
      return {
        ...base,
        text: `${sid}: ${humanize(st).toLowerCase()}${p.human_override ? " (human)" : ""}`,
        tone: st === "accepted" ? "success" : st === "failed" ? "danger" : "warning",
      };
    }
    case "subtask.blocked":
      return { ...base, text: `${sid}: blocked`, tone: "warning" };
    case "escalation.requested":
      return { ...base, text: "Escalated to a human", tone: "warning" };
    case "approval.requested":
      return { ...base, text: `Approval needed: ${humanize(str(p.kind)).toLowerCase()}`, tone: "warning" };
    case "approval.decided":
      return { ...base, text: `Approval ${str(p.action) || str(p.status)}`, tone: "violet" };
    case "action.decided":
      return { ...base, text: `${str(p.tool)}: ${str(p.action)}${p.source ? ` (${str(p.source)})` : ""}`, tone: "violet" };
    case "action.outcome": {
      const st = str(p.status);
      return {
        ...base,
        text: `${str(p.tool)}: ${humanize(st).toLowerCase()}`,
        tone: st === "done" || st === "human_done" ? "success" : st === "failed" || st === "unknown" ? "danger" : "neutral",
      };
    }
    case "final_review.verdict": {
      const v = str(p.verdict);
      return { ...base, text: `Final review ${v} (${num(p.score) ?? "?"}/5)`, tone: v === "accept" ? "success" : "warning" };
    }
    case "budget.warning":
      return { ...base, text: "Budget 80% used", tone: "warning" };
    case "memory.consolidated":
      return { ...base, text: `Saved ${num(p.written) ?? 0} memories`, tone: "neutral", detail: true };
    case "task.status":
      return p.status === "awaiting_approval"
        ? { ...base, text: "Paused for a human decision", tone: "warning" }
        : null;
    case "task.finished": {
      const st = str(p.status);
      return {
        ...base,
        text: `Finished: ${humanize(st).toLowerCase()}`,
        tone: st === "completed" ? "success" : st === "completed_partial" ? "warning" : "danger",
      };
    }
    default:
      return null;
  }
}
