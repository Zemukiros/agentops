import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api } from "../api/client";
import { TERMINAL, type Page, type TaskListItem } from "../api/types";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/Layout";
import { Badge, StatusBadge, TASK_STATUS } from "../components/StatusBadge";
import { Button, Card, EmptyState, ErrorState, Skeleton, inputClass } from "../components/ui";
import { duration, relativeTime, shortId, usd } from "../lib/format";

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function TaskHistory() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const replays = params.get("replays") === "1";
  const [q, setQ] = useState(params.get("q") ?? "");
  const query = useDebounced(q.trim(), 300);

  const set = (k: string, v: string | null) =>
    setParams(
      (p) => {
        if (v) p.set(k, v);
        else p.delete(k);
        return p;
      },
      { replace: true },
    );
  useEffect(() => set("q", query || null), [query]); // eslint-disable-line react-hooks/exhaustive-deps

  const list = useInfiniteQuery({
    queryKey: ["tasks", { status, query, replays }],
    queryFn: ({ pageParam, signal }) =>
      api<Page<TaskListItem>>("/tasks", {
        signal,
        query: { status: status || null, q: query || null, kind: replays ? "all" : "task", cursor: pageParam, limit: 25 },
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((t) => !TERMINAL.has(t.status))) ? 3000 : false,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(status || query || replays);

  return (
    <div>
      <PageHeader
        title="Tasks"
        description="Every run, newest first. Open one to watch it live, inspect its trace or replay it."
        actions={
          <Link
            to="/tasks/new"
            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-accent px-3.5 text-sm font-medium text-accent-fg shadow-card hover:bg-accent-hover"
          >
            <Icon name="plus" /> New task
          </Link>
        }
      />

      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative sm:max-w-xs sm:flex-1">
          <Icon name="search" className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-fg-subtle" />
          <label htmlFor="task-search" className="sr-only">
            Search requests
          </label>
          <input
            id="task-search"
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search requests"
            className={`${inputClass} pl-9`}
          />
        </div>
        <label htmlFor="status-filter" className="sr-only">
          Status
        </label>
        <select
          id="status-filter"
          value={status}
          onChange={(e) => set("status", e.target.value || null)}
          className={`${inputClass} sm:w-48`}
        >
          <option value="">All statuses</option>
          {Object.entries(TASK_STATUS).map(([k, v]) => (
            <option key={k} value={k}>
              {v.label}
            </option>
          ))}
        </select>
        <label className="inline-flex items-center gap-2 text-sm text-fg-muted sm:ml-2">
          <input
            type="checkbox"
            checked={replays}
            onChange={(e) => set("replays", e.target.checked ? "1" : null)}
            className="size-4 accent-[var(--accent)]"
          />
          Include replays
        </label>
      </div>

      <Card bodyClassName="">
        {list.isPending ? (
          <div className="space-y-3 p-4" role="status" aria-label="Loading tasks">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : list.isError ? (
          <div className="p-4">
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          </div>
        ) : items.length === 0 ? (
          filtered ? (
            <EmptyState icon="search" title="No tasks match these filters" />
          ) : (
            <EmptyState
              icon="spark"
              title="No tasks yet"
              action={
                <Link to="/tasks/new" className="text-sm font-medium text-accent-ink hover:underline">
                  Start your first task
                </Link>
              }
            >
              Describe an outcome and watch the agents plan, work and ask for approval.
            </EmptyState>
          )
        ) : (
          <>
            <table className="w-full table-fixed text-sm">
              <caption className="sr-only">Tasks</caption>
              <thead className="hidden border-b border-line bg-surface-2/60 text-left text-xs font-medium text-fg-subtle md:table-header-group">
                <tr>
                  <th scope="col" className="px-4 py-2.5 font-medium">Request</th>
                  <th scope="col" className="w-44 px-3 py-2.5 font-medium">Status</th>
                  <th scope="col" className="w-24 px-3 py-2.5 text-right font-medium">Cost</th>
                  <th scope="col" className="w-24 px-3 py-2.5 text-right font-medium">Duration</th>
                  <th scope="col" className="w-28 px-4 py-2.5 text-right font-medium">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {items.map((t) => (
                  <tr key={t.id} className="group relative block hover:bg-surface-2 md:table-row">
                    <td className="block px-4 pt-3 pb-1 md:table-cell md:py-3">
                      <Link
                        to={`/tasks/${t.id}`}
                        className="block truncate font-medium text-fg after:absolute after:inset-0 group-hover:text-accent-ink"
                      >
                        {t.request}
                      </Link>
                      <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-fg-subtle">
                        <span className="font-mono">{shortId(t.id)}</span>
                        {t.kind === "replay" && (
                          <Badge tone="violet" icon="branch">
                            replay
                          </Badge>
                        )}
                        {t.branches > 0 && (
                          <span className="inline-flex items-center gap-1">
                            <Icon name="branch" size={12} /> {t.branches}
                          </span>
                        )}
                        {t.pending_approvals > 0 && (
                          <span className="inline-flex items-center gap-1 text-warning-fg">
                            <Icon name="hand" size={12} /> {t.pending_approvals} pending
                          </span>
                        )}
                        {t.scenario && <span>{t.scenario}</span>}
                      </div>
                    </td>
                    <td className="inline-block px-4 pb-3 md:table-cell md:px-3 md:py-3">
                      <StatusBadge status={t.status} />
                    </td>
                    <td className="inline-block px-2 pb-3 text-xs text-fg-muted tabular-nums md:table-cell md:px-3 md:py-3 md:text-right md:text-sm">
                      {usd(t.cost_usd)}
                    </td>
                    <td className="inline-block px-2 pb-3 text-xs text-fg-muted tabular-nums md:table-cell md:px-3 md:py-3 md:text-right md:text-sm">
                      {duration(t.duration_s)}
                    </td>
                    <td className="hidden px-4 py-3 text-right text-fg-subtle md:table-cell">
                      <time dateTime={t.created_at}>{relativeTime(t.created_at)}</time>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {list.hasNextPage && (
              <div className="border-t border-line p-3 text-center">
                <Button variant="secondary" size="sm" onClick={() => void list.fetchNextPage()} busy={list.isFetchingNextPage}>
                  Load more
                </Button>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
