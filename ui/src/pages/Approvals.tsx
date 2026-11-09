import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ApiError, api } from "../api/client";
import { useSession } from "../api/session";
import type { Approval } from "../api/types";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/Layout";
import { Badge, StatusBadge } from "../components/StatusBadge";
import { Button, Callout, Card, EmptyState, ErrorState, Skeleton } from "../components/ui";
import { ApprovalContext, KIND_LABEL } from "../features/approvals/ApprovalContext";
import { DecisionForm, initialForm, type Decision, type FormState } from "../features/approvals/DecisionForm";
import { relativeTime, shortId } from "../lib/format";

export function usePendingApprovals(enabled: boolean) {
  return useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: ({ signal }) => api<Approval[]>("/approvals", { signal, query: { state: "pending" } }),
    enabled,
    refetchInterval: 5000,
  });
}

function ApprovalRow({ a, selected, onSelect }: { a: Approval; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      className={`block w-full border-l-2 px-4 py-3 text-left hover:bg-surface-2 ${selected ? "border-accent bg-accent-soft/50" : "border-transparent"}`}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-sm font-semibold text-fg">{KIND_LABEL[a.kind] ?? a.kind}</span>
        {a.kind === "approve_action" && <span className="font-mono text-xs text-fg-muted">{String(a.payload.tool)}</span>}
        {a.task_kind === "replay" && (
          <Badge tone="violet" icon="branch">
            replay
          </Badge>
        )}
        {!a.blocking && <Badge tone="neutral">non-blocking</Badge>}
        {a.status !== "pending" && <StatusBadge status={a.status} kind="approval" />}
      </div>
      <p className="mt-0.5 truncate text-[13px] text-fg-muted">{a.task_request}</p>
      <p className="mt-0.5 text-xs text-fg-subtle">
        {relativeTime(a.created_at)} · task {shortId(a.task_id)}
      </p>
    </button>
  );
}

function Detail({ approval, onDone }: { approval: Approval; onDone: () => void }) {
  const qc = useQueryClient();
  // Form state is keyed by approval id (not version): a 409 reload keeps what was typed.
  const [form, setForm] = useState<FormState>(() => initialForm(approval));
  const [conflict, setConflict] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reviewer = useSession().active?.role === "reviewer";

  const decide = useMutation({
    mutationFn: (decision: Decision) =>
      api<Approval>(`/approvals/${approval.id}/decision`, {
        method: "POST",
        body: { version: approval.version, decision },
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      void qc.invalidateQueries({ queryKey: ["task", approval.task_id] });
      onDone();
    },
    onError: async (e) => {
      if (e instanceof ApiError && e.isConflict) {
        const fresh = await api<Approval>(`/approvals/${approval.id}`).catch(() => null);
        setConflict(
          fresh && fresh.status !== "pending"
            ? `Someone else already decided this (${fresh.status.replace("_", " ")}). Nothing was changed by you.`
            : "This item changed while you were looking at it. Reload to see the latest version; your input is kept.",
        );
        void qc.invalidateQueries({ queryKey: ["approvals"] });
      } else setError(e.message);
    },
  });

  const pending = approval.status === "pending";
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{KIND_LABEL[approval.kind] ?? approval.kind}</h2>
            <StatusBadge status={approval.status} kind="approval" />
            {approval.task_kind === "replay" && (
              <Badge tone="violet" icon="branch">
                from a replay branch
              </Badge>
            )}
          </div>
          <Link to={`/tasks/${approval.task_id}`} className="mt-1 line-clamp-2 text-sm text-accent-ink hover:underline">
            {approval.task_request}
          </Link>
          <p className="mt-1 text-xs text-fg-subtle">
            Requested {relativeTime(approval.created_at)}
            {pending && approval.blocking && <> · expires {relativeTime(approval.expires_at)}</>} · version{" "}
            {approval.version}
          </p>
        </div>
      </div>

      <ApprovalContext approval={approval} />

      {conflict && (
        <Callout tone="warning" icon="refresh" title="Changed by someone else">
          <p>{conflict}</p>
          <Button
            size="sm"
            className="mt-2"
            icon="refresh"
            onClick={() => {
              setConflict(null);
              void qc.invalidateQueries({ queryKey: ["approval", approval.id] });
              void qc.invalidateQueries({ queryKey: ["approvals"] });
            }}
          >
            Reload
          </Button>
        </Callout>
      )}
      {error && (
        <p role="alert" className="rounded-lg border border-danger-line bg-danger-bg px-3 py-2 text-sm text-danger-fg">
          {error}
        </p>
      )}

      {pending ? (
        reviewer ? (
          <div className="border-t border-line pt-4">
            <DecisionForm
              approval={approval}
              form={form}
              setForm={(f) => {
                setError(null);
                setForm(f);
              }}
              onSubmit={(d) => decide.mutate(d)}
              busy={decide.isPending}
            />
          </div>
        ) : (
          <Callout tone="info" icon="shield" title="A reviewer decides this">
            Switch to a reviewer identity to approve, edit, reject or take over.
          </Callout>
        )
      ) : (
        <div className="border-t border-line pt-4 text-sm">
          <h3 className="mb-1.5 text-xs font-semibold tracking-wide text-fg-subtle uppercase">Decision</h3>
          <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-xs">{JSON.stringify(approval.decision, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export function Approvals() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("state") === "decided" ? "decided" : "pending";
  const selectedId = params.get("id");
  const { active } = useSession();
  const list = useQuery({
    queryKey: ["approvals", tab],
    queryFn: ({ signal }) => api<Approval[]>("/approvals", { signal, query: { state: tab, limit: 100 } }),
    refetchInterval: tab === "pending" ? 5000 : false,
  });
  const items = list.data ?? [];
  const selected = items.find((a) => a.id === selectedId) ?? (selectedId ? undefined : items[0]);
  const one = useQuery({
    queryKey: ["approval", selectedId],
    queryFn: () => api<Approval>(`/approvals/${selectedId}`),
    enabled: Boolean(selectedId) && !items.some((a) => a.id === selectedId) && !list.isPending,
  });
  const current = selected ?? one.data;
  const select = (id: string | null, state = tab) =>
    setParams(
      (p) => {
        if (id) p.set("id", id);
        else p.delete("id");
        if (state === "decided") p.set("state", "decided");
        else p.delete("state");
        return p;
      },
      { replace: true },
    );

  return (
    <div>
      <PageHeader
        title="Approvals"
        description={
          active?.role === "reviewer"
            ? "Everything waiting for a human: plans, actions, escalations, budget raises and side-effect checks."
            : "Items from your tasks waiting for a reviewer. Only reviewers can decide them."
        }
      />
      <div className="grid items-start gap-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card bodyClassName="">
          <div role="tablist" aria-label="Approval state" className="flex border-b border-line px-2">
            {(["pending", "decided"] as const).map((t) => (
              <button
                key={t}
                role="tab"
                type="button"
                aria-selected={tab === t}
                onClick={() => select(null, t)}
                className={`-mb-px border-b-2 px-3 py-2.5 text-sm font-medium capitalize ${
                  tab === t ? "border-accent text-fg" : "border-transparent text-fg-muted hover:text-fg"
                }`}
              >
                {t}
                {t === "pending" && list.data && tab === "pending" && <span className="ml-1.5 text-xs text-fg-subtle">{items.length}</span>}
              </button>
            ))}
          </div>
          {list.isPending ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          ) : list.isError ? (
            <div className="p-4">
              <ErrorState error={list.error} onRetry={() => void list.refetch()} />
            </div>
          ) : items.length === 0 ? (
            <EmptyState icon="check" title={tab === "pending" ? "Nothing waiting" : "No decisions yet"}>
              {tab === "pending" ? "New items appear here as soon as a run pauses for a human." : undefined}
            </EmptyState>
          ) : (
            <ul className="max-h-[70vh] divide-y divide-line overflow-y-auto">
              {items.map((a) => (
                <li key={a.id}>
                  <ApprovalRow a={a} selected={current?.id === a.id} onSelect={() => select(a.id)} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card bodyClassName="p-5">
          {current ? (
            <Detail key={current.id} approval={current} onDone={() => select(null)} />
          ) : one.isError ? (
            <ErrorState error={one.error} />
          ) : (
            <EmptyState icon="inbox" title="Select an item">
              Its context and the decision form appear here.
            </EmptyState>
          )}
        </Card>
      </div>
      {tab === "pending" && items.length > 0 && (
        <p className="mt-3 flex items-center gap-1.5 text-xs text-fg-subtle">
          <Icon name="clock" size={12} /> Blocking items left undecided expire and are rejected automatically.
        </p>
      )}
    </div>
  );
}
