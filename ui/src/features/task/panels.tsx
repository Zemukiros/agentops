// Task detail panels: subtasks (results + reviews), proposed actions, deliverable, budget.
import type { ActionView, Budget, SubtaskView, TaskDetail } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Markdown } from "../../components/Markdown";
import { Badge, StatusBadge } from "../../components/StatusBadge";
import { EmptyState } from "../../components/ui";
import { mergeStatus, type LiveSubtask } from "../../lib/dag";
import { compact, usd } from "../../lib/format";

function ScoreDots({ score }: { score: number }) {
  return (
    <span className="inline-flex items-center gap-0.5" role="img" aria-label={`Score ${score} out of 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} aria-hidden className={`size-1.5 rounded-full ${i <= score ? "bg-fg-muted" : "bg-line-strong/60"}`} />
      ))}
    </span>
  );
}

export function SubtaskList({ subtasks, live }: { subtasks: SubtaskView[]; live: Record<string, LiveSubtask> }) {
  if (subtasks.length === 0) return <EmptyState icon="list" title="No plan yet">Subtasks appear once the supervisor has planned the work.</EmptyState>;
  return (
    <ul className="divide-y divide-line">
      {subtasks.map((s) => {
        const status = mergeStatus(s, live[s.id]);
        const last = s.reviews.at(-1);
        return (
          <li key={s.id}>
            <details className="group">
              <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 hover:bg-surface-2 [&::-webkit-details-marker]:hidden">
                <Icon name="chevron" className="shrink-0 text-fg-subtle transition-transform group-open:rotate-90" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-[13px] font-semibold">{s.id}</span>
                    <span className="text-xs text-fg-subtle">{s.specialist}</span>
                    {s.depends_on.length > 0 && (
                      <span className="text-xs text-fg-subtle">after {s.depends_on.join(", ")}</span>
                    )}
                  </div>
                  <p className="mt-0.5 truncate text-[13px] text-fg-muted">{s.description}</p>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  {s.revisions > 0 && <span className="hidden text-xs text-fg-subtle sm:inline">{s.revisions} revision{s.revisions > 1 ? "s" : ""}</span>}
                  {last && <ScoreDots score={last.score} />}
                  <StatusBadge status={status} kind="subtask" />
                </div>
              </summary>
              <div className="space-y-4 border-t border-line bg-surface-2/40 px-4 py-4 sm:pl-11">
                {s.human_override && (
                  <Badge tone="violet" icon="user">
                    Decided by a human
                  </Badge>
                )}
                {s.reason && <p className="text-sm text-warning-fg">{s.reason}</p>}
                <div>
                  <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-fg-subtle uppercase">Result</h4>
                  {s.output ? (
                    <div className="rounded-lg border border-line bg-surface p-3">
                      <Markdown>{s.output}</Markdown>
                    </div>
                  ) : (
                    <p className="text-sm text-fg-subtle">No output yet.</p>
                  )}
                </div>
                {s.reviews.length > 0 && (
                  <div>
                    <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-fg-subtle uppercase">Reviews</h4>
                    <ol className="space-y-2">
                      {s.reviews.map((r) => (
                        <li key={r.revision} className="rounded-lg border border-line bg-surface p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-xs text-fg-subtle">Revision {r.revision}</span>
                            <Badge
                              tone={r.verdict === "accept" ? "success" : r.verdict === "revise" ? "warning" : "danger"}
                              icon={r.verdict === "accept" ? "check" : r.verdict === "revise" ? "refresh" : "alert"}
                            >
                              {r.verdict}
                            </Badge>
                            <span className="text-xs font-medium text-fg-muted">{r.score}/5</span>
                          </div>
                          {r.feedback_for_specialist && <p className="mt-1.5 text-sm text-fg">{r.feedback_for_specialist}</p>}
                          {r.issues.length > 0 && (
                            <ul className="mt-1.5 list-disc pl-5 text-sm text-fg-muted">
                              {r.issues.map((i, n) => (
                                <li key={n}>{i}</li>
                              ))}
                            </ul>
                          )}
                          {r.unsupported_claims.length > 0 && (
                            <p className="mt-1.5 text-xs text-danger-fg">Unsupported: {r.unsupported_claims.join("; ")}</p>
                          )}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
              </div>
            </details>
          </li>
        );
      })}
    </ul>
  );
}

const RISK_TONE = { write: "info", external: "warning", destructive: "danger" } as const;

function ArgValue({ v }: { v: unknown }) {
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return <span className="break-words whitespace-pre-wrap">{text}</span>;
}

export function ActionList({ actions }: { actions: ActionView[] }) {
  if (actions.length === 0)
    return (
      <EmptyState icon="shield" title="No actions proposed">
        Writes outside the task (email, notes) are proposed here and only run after a reviewer approves them.
      </EmptyState>
    );
  return (
    <ul className="divide-y divide-line">
      {actions.map((a) => (
        <li key={a.hash} className="space-y-3 px-4 py-3.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 font-mono text-[13px] font-semibold">
              <Icon name={a.tool === "send_email" ? "mail" : "wrench"} size={14} />
              {a.tool}
            </span>
            <Badge tone={RISK_TONE[a.risk as keyof typeof RISK_TONE] ?? "neutral"}>{a.risk}</Badge>
            <span className="text-xs text-fg-subtle">
              proposed by {a.agent} in {a.subtask_id}
            </span>
            <span className="ml-auto flex items-center gap-2">
              {a.approval && <StatusBadge status={a.approval.status} kind="approval" />}
              {a.outcome && <StatusBadge status={a.outcome.status} kind="outcome" />}
            </span>
          </div>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-lg border border-line bg-surface-2/50 px-3 py-2 text-[13px]">
            {Object.entries(a.args).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="font-mono text-fg-subtle">{k}</dt>
                <dd className="text-fg">
                  <ArgValue v={v} />
                </dd>
              </div>
            ))}
          </dl>
          {(a.outcome?.detail || a.mailpit_url) && (
            <div className="flex flex-wrap items-center gap-3 text-sm">
              {a.outcome?.detail && <span className="text-fg-muted">{a.outcome.detail}</span>}
              {a.mailpit_url && (
                <a
                  href={a.mailpit_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 font-medium text-accent-ink hover:underline"
                >
                  View email in Mailpit <Icon name="external" size={13} />
                </a>
              )}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

export function Deliverable({ task }: { task: TaskDetail }) {
  if (!task.final)
    return (
      <EmptyState icon="spark" title="No deliverable yet">
        The supervisor writes it after the subtasks are accepted and any actions are decided.
      </EmptyState>
    );
  return (
    <div className="space-y-3">
      {task.final_review && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          <span>Final review</span>
          <Badge tone={task.final_review.verdict === "accept" ? "success" : "warning"} icon={task.final_review.verdict === "accept" ? "check" : "alert"}>
            {task.final_review.verdict} · {task.final_review.score}/5
          </Badge>
          {task.partial && (
            <Badge tone="warning" icon="half">
              Partial
            </Badge>
          )}
        </div>
      )}
      <Markdown className="text-[14.5px]">{task.final}</Markdown>
    </div>
  );
}

function Meter({ label, used, max, format }: { label: string; used: number; max: number; format: (n: number) => string }) {
  const pct = max > 0 ? Math.min(100, (used / max) * 100) : 0;
  const tone = pct >= 100 ? "bg-danger-fg" : pct >= 80 ? "bg-warning-fg" : "bg-accent";
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-fg-muted">{label}</span>
        <span className="font-medium text-fg tabular-nums">
          {format(used)} <span className="text-fg-subtle">/ {format(max)}</span>
        </span>
      </div>
      <div
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-3"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={used}
      >
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function BudgetMeters({ budget }: { budget: Budget }) {
  return (
    <div className="space-y-3.5">
      <Meter label="Cost" used={budget.used_usd} max={budget.max_usd} format={(n) => usd(n)} />
      <Meter label="Model calls" used={budget.used_calls} max={budget.max_calls} format={(n) => String(n)} />
      <Meter label="Tokens" used={budget.used_tokens} max={budget.max_tokens} format={compact} />
    </div>
  );
}
