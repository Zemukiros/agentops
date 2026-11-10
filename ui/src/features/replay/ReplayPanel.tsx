// Replay (ADR-040): pick a checkpoint, edit what may be edited there, fork, run, compare.
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../../api/client";
import type { CheckpointFields, CheckpointList, Lineage, Task } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Badge, StatusBadge } from "../../components/StatusBadge";
import { Button, Callout, Card, EmptyState, ErrorState, Field, Skeleton, inputClass, inlineInputClass } from "../../components/ui";
import { nodeLabel } from "../../lib/diff";
import { compact, relativeTime, shortId, usd } from "../../lib/format";
import { hasErrors, validatePlan, type EditablePlan } from "../../lib/planEdit";
import { PlanEditor, planFromPayload, planToPayload } from "../approvals/PlanEditor";

interface ForkForm {
  request: string;
  plan: EditablePlan | null;
  outputs: Record<string, string>;
  final: string;
  budget: { max_usd: string; max_llm_calls: string; max_tokens: string };
}

function fromFields(f: CheckpointFields): ForkForm {
  return {
    request: f.request ?? "",
    plan: f.plan ? planFromPayload(f.plan) : null,
    outputs: { ...f.outputs },
    final: f.final ?? "",
    budget: { max_usd: "", max_llm_calls: "", max_tokens: "" },
  };
}

/** Only fields allowed at this fork point, and only ones that changed. Pure. */
export function buildFork(f: CheckpointFields, form: ForkForm): { body?: Record<string, unknown>; error?: string } {
  const edits: Record<string, unknown> = {};
  if (f.editable.includes("request") && form.request.trim() && form.request !== f.request) edits.request = form.request.trim();
  if (f.editable.includes("plan") && form.plan) {
    if (hasErrors(validatePlan(form.plan))) return { error: "Fix the plan errors first." };
    const original = f.plan ? JSON.stringify(planToPayload(planFromPayload(f.plan))) : null;
    const edited = JSON.stringify(planToPayload(form.plan));
    if (edited !== original) edits.plan = planToPayload(form.plan);
  }
  if (f.editable.includes("outputs")) {
    const changed = Object.fromEntries(Object.entries(form.outputs).filter(([k, v]) => v !== f.outputs[k] && v.trim()));
    if (Object.keys(changed).length) edits.outputs = changed;
  }
  if (f.editable.includes("final") && form.final.trim() && form.final !== f.final) edits.final = form.final;
  const budget: Record<string, number> = {};
  for (const [k, v] of Object.entries(form.budget)) {
    if (!v.trim()) continue;
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return { error: `Budget ${k.replace("_", " ")} must be a positive number.` };
    budget[k] = n;
  }
  return {
    body: {
      checkpoint_id: f.checkpoint_id,
      edits,
      ...(Object.keys(budget).length ? { budget } : {}),
    },
  };
}

function ForkPanel({ taskId, checkpointId, defaults }: { taskId: string; checkpointId: string; defaults: CheckpointList["default_budget"] }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const fields = useQuery({
    queryKey: ["task", taskId, "checkpoint", checkpointId],
    queryFn: () => api<CheckpointFields>(`/tasks/${taskId}/checkpoints/${checkpointId}`),
  });
  const [form, setForm] = useState<ForkForm | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fork = useMutation({
    mutationFn: (body: Record<string, unknown>) => api<Task>(`/tasks/${taskId}/branches`, { method: "POST", body }),
    onSuccess: (t) => {
      void qc.invalidateQueries({ queryKey: ["task", taskId] });
      void qc.invalidateQueries({ queryKey: ["tasks"] });
      navigate(`/tasks/${t.id}`);
    },
    onError: (e) => setError(e.message),
  });

  if (fields.isPending) return <Skeleton className="h-48" />;
  if (fields.isError) return <ErrorState error={fields.error} />;
  const f = fields.data;
  const state = form ?? fromFields(f);
  const set = (patch: Partial<ForkForm>) => setForm({ ...state, ...patch });

  function submit(e: FormEvent) {
    e.preventDefault();
    const r = buildFork(f, state);
    setError(r.error ?? null);
    if (r.body) fork.mutate(r.body);
  }

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <div>
        <p className="text-sm font-semibold">{nodeLabel(f.next[0] ?? "")}</p>
        <p className="mt-0.5 text-xs text-fg-subtle">
          Step {f.step} · wave {f.wave} · checkpoint <span className="font-mono">{f.checkpoint_id.slice(-8)}</span>
        </p>
      </div>
      <Callout tone="violet" icon="branch" title="What the branch reuses">
        Everything before this point (plan, accepted results, tool calls, reviews) is copied, not re-run. Only later steps
        run again. Approved actions are never re-sent: the branch reports what the original did.
      </Callout>

      {f.editable.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing is editable at this point; the branch re-runs the later steps as they are.</p>
      ) : (
        <fieldset className="space-y-4">
          <legend className="mb-2 text-sm font-semibold">Edit before forking (optional)</legend>
          {f.editable.includes("request") && (
            <Field label="Request" htmlFor="fork-request" hint="Planning re-runs from the new request.">
              <textarea id="fork-request" rows={4} value={state.request} onChange={(e) => set({ request: e.target.value })} className={inputClass} />
            </Field>
          )}
          {f.editable.includes("plan") && state.plan && <PlanEditor plan={state.plan} onChange={(plan) => set({ plan })} />}
          {f.editable.includes("outputs") &&
            (Object.keys(state.outputs).length === 0 ? (
              <p className="text-sm text-fg-muted">No accepted outputs yet at this point.</p>
            ) : (
              Object.keys(state.outputs).map((sid) => (
                <Field key={sid} label={`Accepted output of ${sid}`} htmlFor={`out-${sid}`} hint="Downstream subtasks and the deliverable will use your version.">
                  <textarea
                    id={`out-${sid}`}
                    rows={6}
                    value={state.outputs[sid]}
                    onChange={(e) => set({ outputs: { ...state.outputs, [sid]: e.target.value } })}
                    className={inputClass}
                  />
                </Field>
              ))
            ))}
          {f.editable.includes("final") && (
            <Field label="Deliverable" htmlFor="fork-final" hint="The final review re-runs on your version.">
              <textarea id="fork-final" rows={8} value={state.final} onChange={(e) => set({ final: e.target.value })} className={inputClass} />
            </Field>
          )}
        </fieldset>
      )}

      <fieldset>
        <legend className="text-sm font-semibold">Budget</legend>
        <p className="mt-1 mb-3 text-xs text-fg-muted">
          Defaults to the original task’s limits: <strong className="text-fg">{usd(defaults.max_usd)}</strong>,{" "}
          <strong className="text-fg">{defaults.max_llm_calls}</strong> model calls,{" "}
          <strong className="text-fg">{compact(defaults.max_tokens)}</strong> tokens. The branch has its own ledger.
        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          {(
            [
              ["max_usd", "Max cost (USD)", defaults.max_usd],
              ["max_llm_calls", "Max model calls", defaults.max_llm_calls],
              ["max_tokens", "Max tokens", defaults.max_tokens],
            ] as const
          ).map(([k, l, d]) => (
            <Field key={k} label={l} htmlFor={`fork-${k}`}>
              <input
                id={`fork-${k}`}
                type="number"
                min="0"
                value={state.budget[k]}
                onChange={(e) => set({ budget: { ...state.budget, [k]: e.target.value } })}
                placeholder={String(d)}
                className={inputClass}
              />
            </Field>
          ))}
        </div>
      </fieldset>

      {error && (
        <p role="alert" className="rounded-lg border border-danger-line bg-danger-bg px-3 py-2 text-sm text-danger-fg">
          {error}
        </p>
      )}
      <div className="flex justify-end">
        <Button type="submit" variant="primary" icon="branch" busy={fork.isPending}>
          Fork and run
        </Button>
      </div>
    </form>
  );
}

export function ReplayPanel({ taskId }: { taskId: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [compareWith, setCompareWith] = useState<string>("");
  const pages = useInfiniteQuery({
    queryKey: ["task", taskId, "checkpoints"],
    queryFn: ({ pageParam }) =>
      api<CheckpointList & { next_cursor: string | null }>(`/tasks/${taskId}/checkpoints`, {
        query: { limit: 100, cursor: pageParam },
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const lineage = useQuery({ queryKey: ["task", taskId, "lineage"], queryFn: () => api<Lineage>(`/tasks/${taskId}/lineage`) });

  if (pages.isPending) return <Skeleton className="h-64" />;
  if (pages.isError) return <ErrorState error={pages.error} onRetry={() => void pages.refetch()} />;
  // one merged view; pages arrive newest first
  const cps = {
    data: { ...pages.data.pages[0]!, checkpoints: pages.data.pages.flatMap((p) => p.checkpoints) },
  };
  const list = [...cps.data.checkpoints].reverse(); // oldest first, like a timeline
  const shown = showAll ? list : list.filter((c) => c.forkable);
  const others = (lineage.data?.tasks ?? []).filter((t) => t.id !== taskId);
  const current = selected ?? null;

  return (
    <div className="space-y-4">
      {cps.data.fork_blocked_reason && (
        <Callout tone="warning" icon="clock" title="Can’t fork right now">
          {cps.data.fork_blocked_reason}. Forking a running task would race the live run.
        </Callout>
      )}
      <div className="grid items-start gap-4 lg:grid-cols-[340px_minmax(0,1fr)]">
        <Card
          title="Checkpoints"
          bodyClassName=""
          actions={
            <label className="inline-flex items-center gap-1.5 text-xs text-fg-muted">
              <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} className="size-3.5 accent-[var(--accent)]" />
              All steps
            </label>
          }
        >
          {shown.length === 0 ? (
            <EmptyState icon="branch" title="No fork points yet" />
          ) : (
            <>
            {pages.hasNextPage && (
              <div className="border-b border-line px-4 py-2 text-center">
                <Button size="sm" variant="ghost" onClick={() => void pages.fetchNextPage()} busy={pages.isFetchingNextPage}>
                  Load earlier steps
                </Button>
              </div>
            )}
            <ol className="relative py-2" aria-label="Checkpoints">
              {shown.map((c) => {
                const accepted = Object.values(c.subtasks).filter((s) => s === "accepted").length;
                const total = Object.keys(c.subtasks).length;
                const disabled = !c.forkable || Boolean(cps.data.fork_blocked_reason);
                return (
                  <li key={c.checkpoint_id}>
                    <button
                      type="button"
                      disabled={disabled}
                      onClick={() => setSelected(c.checkpoint_id)}
                      aria-current={current === c.checkpoint_id ? "true" : undefined}
                      className={`flex w-full items-start gap-3 px-4 py-2.5 text-left disabled:cursor-not-allowed ${
                        current === c.checkpoint_id ? "bg-accent-soft/60" : "enabled:hover:bg-surface-2"
                      }`}
                    >
                      <span
                        aria-hidden
                        className={`mt-1.5 size-2.5 shrink-0 rounded-full border-2 ${c.forkable ? "border-accent bg-surface" : "border-line-strong bg-surface-3"}`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className={`block text-sm ${c.forkable ? "font-medium text-fg" : "text-fg-subtle"}`}>{nodeLabel(c.next[0] ?? "end")}</span>
                        <span className="block text-xs text-fg-subtle">
                          step {c.step}
                          {total > 0 && ` · ${accepted}/${total} accepted`}
                          {c.has_final && " · deliverable"}
                          {!c.forkable && c.reason && ` · ${c.reason}`}
                        </span>
                      </span>
                      {c.forkable && <Icon name="chevron" className="mt-1 shrink-0 text-fg-subtle" />}
                    </button>
                  </li>
                );
              })}
            </ol>
            </>
          )}
        </Card>

        <Card title="Fork" bodyClassName="p-5">
          {current ? (
            <ForkPanel key={current} taskId={taskId} checkpointId={current} defaults={cps.data.default_budget} />
          ) : (
            <EmptyState icon="branch" title="Pick a checkpoint">
              Fork the run at that point, optionally change the plan, an accepted result or the deliverable, and compare
              what happens next.
            </EmptyState>
          )}
        </Card>
      </div>

      <Card title="Branches" bodyClassName="">
        {lineage.isPending ? (
          <Skeleton className="m-4 h-12" />
        ) : others.length === 0 ? (
          <EmptyState icon="branch" title="No other branches yet" />
        ) : (
          <>
            <ul className="divide-y divide-line">
              {(lineage.data?.tasks ?? []).map((t) => (
                <li key={t.id} className="flex flex-wrap items-center gap-3 px-4 py-2.5">
                  <Badge tone={t.kind === "replay" ? "violet" : "neutral"} icon={t.kind === "replay" ? "branch" : "list"}>
                    {t.kind === "replay" ? "branch" : "original"}
                  </Badge>
                  <Link to={`/tasks/${t.id}`} className="font-mono text-sm text-accent-ink hover:underline">
                    {shortId(t.id)}
                  </Link>
                  {t.parent_task_id && <span className="text-xs text-fg-subtle">from {shortId(t.parent_task_id)}</span>}
                  {t.id === taskId && <span className="text-xs text-fg-subtle">(this task)</span>}
                  <span className="ml-auto flex items-center gap-3">
                    <span className="text-xs text-fg-subtle">{relativeTime(t.created_at)}</span>
                    <StatusBadge status={t.status} />
                  </span>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
              <label htmlFor="compare-with" className="text-sm text-fg-muted">
                Compare this task with
              </label>
              <select id="compare-with" value={compareWith} onChange={(e) => setCompareWith(e.target.value)} className={`${inlineInputClass} h-8 w-56 py-1 text-xs`}>
                <option value="">choose…</option>
                {others.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.kind === "replay" ? "branch" : "original"} {shortId(t.id)}
                  </option>
                ))}
              </select>
              <Link
                to={compareWith ? `/compare/${taskId}/${compareWith}` : "#"}
                aria-disabled={!compareWith}
                onClick={(e) => !compareWith && e.preventDefault()}
                className={`inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium ${
                  compareWith ? "border-line-strong bg-surface text-fg hover:bg-surface-2" : "cursor-not-allowed border-line text-fg-subtle"
                }`}
              >
                Compare side by side
              </Link>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
