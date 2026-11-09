import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client";
import { useTaskEvents } from "../api/events";
import { TERMINAL, type TaskDetail as Detail } from "../api/types";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/Layout";
import { Badge, StatusBadge } from "../components/StatusBadge";
import { Callout, Card, ErrorState, LoadingBlock, Skeleton } from "../components/ui";
import { ActivityFeed } from "../features/task/ActivityFeed";
import { ActionList, BudgetMeters, Deliverable, SubtaskList } from "../features/task/panels";
import { PlanDag } from "../features/task/PlanDag";
import { liveSubtasks, liveTaskStatus, planFromEvents, subtaskRows } from "../lib/dag";
import { duration, relativeTime, shortId, usd } from "../lib/format";
import { useNow } from "../lib/useNow";

// Events after which the snapshot (outputs, reviews, actions, deliverable) is worth refetching.
const REFRESH_ON = new Set([
  "plan.created",
  "subtask.finished",
  "subtask.blocked",
  "review.verdict",
  "approval.requested",
  "approval.decided",
  "action.decided",
  "action.outcome",
  "node.completed",
  "final_review.verdict",
  "task.status",
  "task.finished",
]);

export function TaskDetailPage() {
  const { id } = useParams();
  return <TaskDetailView key={id} id={id!} />;
}

function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(id).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-xs text-fg-subtle hover:bg-surface-2 hover:text-fg"
      aria-label={`Copy task id ${id}`}
    >
      {shortId(id)} <Icon name={copied ? "check" : "copy"} size={12} />
    </button>
  );
}

function TaskDetailView({ id }: { id: string }) {
  const qc = useQueryClient();
  const detail = useQuery({
    queryKey: ["task", id, "detail"],
    queryFn: ({ signal }) => api<Detail>(`/tasks/${id}/detail`, { signal }),
    refetchInterval: (q) => (q.state.data && TERMINAL.has(q.state.data.status) ? false : 10_000),
  });
  const { events, state } = useTaskEvents(id);
  const now = useNow(1000, Boolean(detail.data && !TERMINAL.has(detail.data.status)));

  // Refetch the snapshot shortly after meaningful events (debounced: bursts -> one request).
  const seen = useRef(0);
  useEffect(() => {
    const fresh = events.filter((e) => e.id > seen.current);
    if (fresh.length === 0) return;
    seen.current = events.at(-1)!.id;
    if (!fresh.some((e) => REFRESH_ON.has(e.type))) return;
    const t = setTimeout(() => void qc.invalidateQueries({ queryKey: ["task", id] }), 250);
    return () => clearTimeout(t);
  }, [events, id, qc]);

  const live = useMemo(() => liveSubtasks(events), [events]);
  const plan = useMemo(() => {
    const fromEvents = planFromEvents(events);
    if (fromEvents) return fromEvents;
    return (detail.data?.subtasks ?? []).map((s) => ({
      id: s.id,
      description: s.description,
      specialist: s.specialist,
      depends_on: s.depends_on,
    }));
  }, [events, detail.data]);

  if (detail.isPending)
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-2/3" />
        <Skeleton className="h-4 w-1/3" />
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <Skeleton className="h-72" />
          <Skeleton className="h-72" />
        </div>
      </div>
    );
  if (detail.isError) return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />;
  // the stream is newer than the snapshot between refetches
  const t = { ...detail.data, status: (liveTaskStatus(events) ?? detail.data.status) as Detail["status"] };
  const rows = subtaskRows(plan, t.subtasks);
  const finished = TERMINAL.has(t.status);
  const deliverableCard = (
    <Card id="deliverable" title="Deliverable">
      <Deliverable task={t} />
    </Card>
  );
  const pending = t.approvals.filter((a) => a.status === "pending");
  const wall = (new Date(TERMINAL.has(t.status) ? t.updated_at : now).getTime() - new Date(t.created_at).getTime()) / 1000;

  return (
    <div>
      <nav aria-label="Breadcrumb" className="mb-3 text-xs text-fg-subtle">
        <Link to="/tasks" className="hover:text-fg hover:underline">
          Tasks
        </Link>
        <span aria-hidden> / </span>
        <span>{shortId(t.id)}</span>
      </nav>
      <PageHeader
        title={<span className="line-clamp-2">{t.request}</span>}
        meta={
          <>
            <StatusBadge status={t.status} />
            {t.kind === "replay" && (
              <Link to={`/tasks/${t.parent_task_id}`} className="rounded-full">
                <Badge tone="violet" icon="branch">
                  Replay of {shortId(t.parent_task_id ?? "")}
                </Badge>
              </Link>
            )}
            {t.scenario && <Badge tone="neutral">scenario: {t.scenario}</Badge>}
            <span className="text-xs text-fg-subtle">
              {relativeTime(t.created_at)} · {duration(wall)} · {usd(t.budget?.used_usd)}
            </span>
            <CopyId id={t.id} />
          </>
        }
      />

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0 space-y-4">
          {pending.length > 0 && (
            <Callout tone="warning" icon="hand" title={`Waiting for a reviewer (${pending.length})`}>
              {pending.map((a) => a.kind.replace(/_/g, " ")).join(", ")}. A reviewer decides in the{" "}
              <Link to={`/approvals?id=${pending[0]!.id}`} className="font-medium underline">
                approvals inbox
              </Link>
              ; the run resumes on its own afterwards.
            </Callout>
          )}
          {t.explanation && (
            <Callout
              tone={t.status === "completed_partial" ? "warning" : "danger"}
              icon={t.status === "completed_partial" ? "half" : "alert"}
              title={t.status === "completed_partial" ? "Partial result" : "Why this run stopped"}
            >
              {t.explanation}
            </Callout>
          )}

          <Card
            id="plan"
            title="Plan"
            actions={
              t.plan_confidence && (
                <span className="text-xs text-fg-subtle">
                  confidence {t.plan_confidence.effective.toFixed(2)}
                  <span className="hidden sm:inline"> (self {t.plan_confidence.self_reported.toFixed(2)}, structure {t.plan_confidence.structural.toFixed(2)})</span>
                </span>
              )
            }
          >
            {plan.length > 0 ? (
              <PlanDag plan={plan} snapshot={t.subtasks} live={live} />
            ) : (
              <div className="grid h-40 place-items-center rounded-lg border border-dashed border-line text-sm text-fg-subtle">
                {TERMINAL.has(t.status) ? "This run ended before a plan was made." : "Planning…"}
              </div>
            )}
          </Card>

          {/* finished: the result first; while running: the work first */}
          {finished && deliverableCard}
          <Card id="subtasks" title="Subtasks" bodyClassName="">
            <SubtaskList subtasks={rows} live={live} />
          </Card>

          <Card id="actions" title="Proposed actions" bodyClassName="">
            <ActionList actions={t.actions} />
          </Card>
          {!finished && deliverableCard}

        </div>

        <div className="min-w-0 space-y-4 lg:sticky lg:top-6">
          <Card id="activity" title="Activity" bodyClassName="">
            <ActivityFeed events={events} state={state} />
          </Card>
          {t.budget && (
            <Card id="budget" title="Budget">
              <BudgetMeters budget={t.budget} />
            </Card>
          )}
          {t.memories.length > 0 && (
            <Card id="memories" title="Recalled from past tasks">
              <p className="mb-2 text-xs text-fg-subtle">Past examples, may not apply.</p>
              <ul className="space-y-2">
                {t.memories.map((m) => (
                  <li key={m.id} className="rounded-lg border border-line p-2.5 text-[13px]">
                    <div className="mb-1 flex items-center gap-2 text-xs text-fg-subtle">
                      <span>{m.kind}</span>
                      <span>similarity {m.similarity.toFixed(2)}</span>
                    </div>
                    <p className="line-clamp-3 text-fg">{m.text}</p>
                  </li>
                ))}
              </ul>
            </Card>
          )}
          {detail.isFetching && <LoadingBlock label="Refreshing" rows={0} />}
        </div>
      </div>
    </div>
  );
}
