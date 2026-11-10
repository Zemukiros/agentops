import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api } from "../api/client";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/Layout";
import { Badge, StatusBadge } from "../components/StatusBadge";
import { Button, Card, EmptyState, ErrorState, Field, Skeleton, inputClass } from "../components/ui";
import { relativeTime, shortId } from "../lib/format";

export interface Memory {
  id: string;
  kind: string;
  text: string;
  importance: number;
  outcome: string | null;
  source_task_id: string | null;
  created_at: string;
  similarity: number | null;
  score: number | null;
}

const PAGE = 30;
export const CONFIRM_PHRASE = "delete all";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function KindBadge({ kind }: { kind: string }) {
  return (
    <Badge tone={kind === "episode" ? "neutral" : "info"} icon={kind === "episode" ? "clock" : "brain"}>
      {kind}
    </Badge>
  );
}

function DeleteAllDialog({ count, onClose }: { count: number; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [typed, setTyped] = useState("");
  const qc = useQueryClient();
  const del = useMutation({
    mutationFn: () => api<{ deleted: number }>("/memories", { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["memories"] });
      onClose();
    },
  });
  useEffect(() => {
    ref.current?.showModal(); // native modal: focus trap, Escape closes, inert background
  }, []);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="del-all-title"
      className="m-auto w-[min(440px,calc(100vw-2rem))] rounded-2xl border border-line bg-surface p-0 text-fg shadow-pop backdrop:bg-black/50"
    >
      <form
        method="dialog"
        className="space-y-4 p-6"
        onSubmit={(e) => {
          e.preventDefault();
          if (typed.trim().toLowerCase() === CONFIRM_PHRASE) del.mutate();
        }}
      >
        <div className="flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-full bg-danger-bg text-danger-fg">
            <Icon name="trash" />
          </span>
          <div>
            <h2 id="del-all-title" className="text-base font-semibold">
              Delete all memories?
            </h2>
            <p className="mt-1 text-sm text-fg-muted">
              This permanently removes {count > 0 ? `all ${count}+` : "all"} of your memories. Future tasks start without
              past examples. Tasks and their history are not affected.
            </p>
          </div>
        </div>
        <Field label={`Type “${CONFIRM_PHRASE}” to confirm`} htmlFor="confirm-phrase">
          <input id="confirm-phrase" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} className={inputClass} />
        </Field>
        {del.isError && <p className="text-sm text-danger-fg">{del.error.message}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => ref.current?.close()}>
            Cancel
          </Button>
          <Button type="submit" variant="danger" icon="trash" disabled={typed.trim().toLowerCase() !== CONFIRM_PHRASE} busy={del.isPending}>
            Delete all
          </Button>
        </div>
      </form>
    </dialog>
  );
}

function MemoryDetail({ id, onDeleted }: { id: string; onDeleted: () => void }) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const m = useQuery({ queryKey: ["memories", "one", id], queryFn: () => api<Memory>(`/memories/${id}`) });
  const del = useMutation({
    mutationFn: () => api<void>(`/memories/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["memories"] });
      onDeleted();
    },
  });
  if (m.isPending) return <Skeleton className="h-40" />;
  if (m.isError) return <ErrorState error={m.error} />;
  const x = m.data;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <KindBadge kind={x.kind} />
        {x.outcome && <StatusBadge status={x.outcome} />}
        <span className="text-xs text-fg-subtle">importance {x.importance.toFixed(2)}</span>
      </div>
      <p className="text-sm leading-relaxed whitespace-pre-wrap text-fg">{x.text}</p>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-fg-subtle">Saved</dt>
        <dd>{new Date(x.created_at).toLocaleString()}</dd>
        <dt className="text-fg-subtle">Source task</dt>
        <dd>
          {x.source_task_id ? (
            <Link to={`/tasks/${x.source_task_id}`} className="font-mono text-accent-ink hover:underline">
              {shortId(x.source_task_id)}
            </Link>
          ) : (
            "—"
          )}
        </dd>
        <dt className="text-fg-subtle">Id</dt>
        <dd className="font-mono text-xs break-all text-fg-muted">{x.id}</dd>
      </dl>
      <div className="border-t border-line pt-4">
        {confirm ? (
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Confirm delete">
            <span className="text-sm text-fg">Delete this memory?</span>
            <Button size="sm" variant="danger" onClick={() => del.mutate()} busy={del.isPending}>
              Delete
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>
              Keep it
            </Button>
          </div>
        ) : (
          <Button size="sm" variant="danger" icon="trash" onClick={() => setConfirm(true)}>
            Delete memory
          </Button>
        )}
        {del.isError && <p className="mt-2 text-sm text-danger-fg">{del.error.message}</p>}
      </div>
    </div>
  );
}

export function MemoryPage() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [weak, setWeak] = useState(false);
  const [deleteAll, setDeleteAll] = useState(false);
  const query = useDebounced(q.trim(), 350);
  const selected = params.get("id");
  const select = (id: string | null) =>
    setParams(
      (p) => {
        if (id) p.set("id", id);
        else p.delete("id");
        return p;
      },
      { replace: true },
    );

  const list = useInfiniteQuery({
    queryKey: ["memories", "list"],
    queryFn: ({ pageParam }) => api<Memory[]>("/memories", { query: { limit: PAGE, offset: pageParam } }),
    initialPageParam: 0,
    getNextPageParam: (last, all) => (last.length === PAGE ? all.length * PAGE : undefined),
    enabled: !query,
  });
  const search = useQuery({
    queryKey: ["memories", "search", query, weak],
    queryFn: () => api<Memory[]>("/memories/search", { query: { q: query, k: 20, min_similarity: weak ? 0.3 : null } }),
    enabled: Boolean(query),
  });
  const active = query ? search : list;
  const items = query ? (search.data ?? []) : (list.data?.pages.flat() ?? []);

  return (
    <div>
      <PageHeader
        title="Memory"
        description="What the agents remember across tasks: episodes (how a task ended) and lessons from accepted results. Recalled memories are shown to the planner as “past examples, may not apply”."
        actions={
          <Button variant="danger" icon="trash" onClick={() => setDeleteAll(true)} disabled={!query && items.length === 0}>
            Delete all
          </Button>
        }
      />
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative sm:max-w-md sm:flex-1">
          <Icon name="search" className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-fg-subtle" />
          <label htmlFor="memory-search" className="sr-only">
            Search memories
          </label>
          <input
            id="memory-search"
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by meaning, e.g. “vector database”"
            className={`${inputClass} pl-9`}
          />
        </div>
        {query && (
          <label className="inline-flex items-center gap-2 text-sm text-fg-muted">
            <input type="checkbox" checked={weak} onChange={(e) => setWeak(e.target.checked)} className="size-4 accent-[var(--accent)]" />
            Include weaker matches
          </label>
        )}
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <Card bodyClassName="">
          {active.isPending && (query || !list.data) ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          ) : active.isError ? (
            <div className="p-4">
              <ErrorState error={active.error} onRetry={() => void active.refetch()} />
            </div>
          ) : items.length === 0 ? (
            query ? (
              <EmptyState icon="search" title="No similar memories">
                {weak ? "Nothing above the minimum similarity." : "Try including weaker matches."}
              </EmptyState>
            ) : (
              <EmptyState icon="brain" title="No memories yet">
                They’re saved when a task finishes: an episode always, lessons only from accepted results.
              </EmptyState>
            )
          ) : (
            <>
              <ul className="divide-y divide-line" aria-label="Memories">
                {items.map((m) => (
                  <li key={m.id}>
                    <button
                      type="button"
                      onClick={() => select(m.id)}
                      aria-current={selected === m.id ? "true" : undefined}
                      className={`block w-full border-l-2 px-4 py-3 text-left hover:bg-surface-2 ${selected === m.id ? "border-accent bg-accent-soft/50" : "border-transparent"}`}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <KindBadge kind={m.kind} />
                        {m.outcome && <StatusBadge status={m.outcome} />}
                        {m.similarity !== null && <span className="text-xs text-fg-subtle tabular-nums">similarity {m.similarity.toFixed(2)}</span>}
                        <span className="ml-auto text-xs text-fg-subtle">{relativeTime(m.created_at)}</span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-[13px] text-fg">{m.text}</p>
                    </button>
                  </li>
                ))}
              </ul>
              {!query && list.hasNextPage && (
                <div className="border-t border-line p-3 text-center">
                  <Button size="sm" onClick={() => void list.fetchNextPage()} busy={list.isFetchingNextPage}>
                    Load more
                  </Button>
                </div>
              )}
            </>
          )}
        </Card>
        <Card title="Inspect" bodyClassName="p-4">
          {selected ? (
            <MemoryDetail key={selected} id={selected} onDeleted={() => select(null)} />
          ) : (
            <EmptyState icon="brain" title="Select a memory" />
          )}
        </Card>
      </div>
      {deleteAll && <DeleteAllDialog count={items.length} onClose={() => setDeleteAll(false)} />}
    </div>
  );
}
