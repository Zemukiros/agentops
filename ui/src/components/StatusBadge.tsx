// Status badges: color + icon + text, never color alone (WCAG 1.4.1).
import { Icon, type IconName } from "./Icon";

export type Tone = "info" | "success" | "warning" | "danger" | "neutral" | "violet";

const TONE: Record<Tone, string> = {
  info: "bg-info-bg text-info-fg border-info-line",
  success: "bg-success-bg text-success-fg border-success-line",
  warning: "bg-warning-bg text-warning-fg border-warning-line",
  danger: "bg-danger-bg text-danger-fg border-danger-line",
  neutral: "bg-neutral-bg text-neutral-fg border-neutral-line",
  violet: "bg-violet-bg text-violet-fg border-violet-line",
};

interface StatusSpec {
  label: string;
  tone: Tone;
  icon: IconName;
  live?: boolean;
}

export const TASK_STATUS: Record<string, StatusSpec> = {
  queued: { label: "Queued", tone: "neutral", icon: "clock" },
  running: { label: "Running", tone: "info", icon: "dot", live: true },
  awaiting_approval: { label: "Awaiting approval", tone: "warning", icon: "hand" },
  completed: { label: "Completed", tone: "success", icon: "check" },
  completed_partial: { label: "Partial", tone: "warning", icon: "half" },
  failed: { label: "Failed", tone: "danger", icon: "x" },
  rejected: { label: "Rejected", tone: "neutral", icon: "ban" },
  budget_exceeded: { label: "Budget exceeded", tone: "danger", icon: "coins" },
  cancelled: { label: "Cancelled", tone: "neutral", icon: "stop" },
};

export const SUBTASK_STATUS: Record<string, StatusSpec> = {
  pending: { label: "Pending", tone: "neutral", icon: "clock" },
  dispatched: { label: "Dispatched", tone: "info", icon: "dot", live: true },
  working: { label: "Working", tone: "info", icon: "dot", live: true },
  in_review: { label: "In review", tone: "info", icon: "dot", live: true },
  revising: { label: "Revising", tone: "warning", icon: "refresh", live: true },
  accepted: { label: "Accepted", tone: "success", icon: "check" },
  failed: { label: "Failed", tone: "danger", icon: "x" },
  needs_human: { label: "Needs human", tone: "warning", icon: "hand" },
  blocked: { label: "Blocked", tone: "neutral", icon: "ban" },
};

export const OUTCOME_STATUS: Record<string, StatusSpec> = {
  done: { label: "Done", tone: "success", icon: "check" },
  human_done: { label: "Done by a human", tone: "success", icon: "user" },
  failed: { label: "Failed", tone: "danger", icon: "x" },
  unknown: { label: "Unknown", tone: "danger", icon: "alert" },
  rejected: { label: "Not executed", tone: "neutral", icon: "ban" },
  not_considered: { label: "Not executed", tone: "neutral", icon: "ban" },
};

export const APPROVAL_STATUS: Record<string, StatusSpec> = {
  pending: { label: "Pending", tone: "warning", icon: "hand" },
  approved: { label: "Approved", tone: "success", icon: "check" },
  edited: { label: "Edited", tone: "violet", icon: "check" },
  rejected: { label: "Rejected", tone: "neutral", icon: "ban" },
  taken_over: { label: "Taken over", tone: "violet", icon: "user" },
  expired: { label: "Expired", tone: "neutral", icon: "clock" },
};

const FALLBACK: StatusSpec = { label: "", tone: "neutral", icon: "dot" };

export function Badge({
  tone,
  icon,
  children,
  live,
  className = "",
}: {
  tone: Tone;
  icon?: IconName;
  children: React.ReactNode;
  live?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${TONE[tone]} ${className}`}
    >
      {icon && <Icon name={icon} size={12} className={live ? "animate-pulse-dot" : undefined} />}
      {children}
    </span>
  );
}

export function StatusBadge({
  status,
  kind = "task",
}: {
  status: string;
  kind?: "task" | "subtask" | "outcome" | "approval";
}) {
  const table =
    kind === "subtask"
      ? SUBTASK_STATUS
      : kind === "outcome"
        ? OUTCOME_STATUS
        : kind === "approval"
          ? APPROVAL_STATUS
          : TASK_STATUS;
  const spec = table[status] ?? { ...FALLBACK, label: status.replace(/_/g, " ") };
  return (
    <Badge tone={spec.tone} icon={spec.icon} live={spec.live}>
      {spec.label}
    </Badge>
  );
}
