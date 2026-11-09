// Trace & cost: totals, compute vs human-wait vs queue time, per-agent/model/tool tables,
// and one timeline of events + model calls + tool calls (redacted, truncated server-side).
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { CostBucket, CostSummary, TraceItem } from "../../api/types";
import { Icon, type IconName } from "../../components/Icon";
import { Badge } from "../../components/StatusBadge";
import { Button, Card, EmptyState, ErrorState, Skeleton, inputClass } from "../../components/ui";
import { clockTime, compact, duration, usd } from "../../lib/format";

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-line bg-surface p-4 shadow-card">
      <p className="text-xs font-medium text-fg-subtle">{label}</p>
      <p className="mt-1 text-xl font-semibold tracking-tight text-fg tabular-nums">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-fg-muted">{sub}</p>}
    </div>
  );
}

/** Stacked bar: where the wall-clock time went. Legend carries the numbers (identity is
 * never color alone); 2px surface gaps between segments. */
export function TimeBar({ time }: { time: CostSummary["time"] }) {
  const parts = [
    { key: "compute", label: "Compute", s: time.compute_s, cls: "bg-viz-1" },
    { key: "human", label: "Human wait", s: time.human_wait_s, cls: "bg-viz-2" },
    { key: "queue", label: "Queue", s: time.queue_s, cls: "bg-viz-3" },
  ];
  const total = Math.max(time.wall_s, parts.reduce((a, p) => a + p.s, 0), 0.001);
  return (
    <div>
      <div className="flex h-3 w-full gap-[2px] overflow-hidden rounded-full bg-surface-3" role="img" aria-label={parts.map((p) => `${p.label} ${duration(p.s)}`).join(", ")}>
        {parts
          .filter((p) => p.s > 0)
          .map((p) => (
            <div key={p.key} className={`${p.cls} h-full first:rounded-l-full last:rounded-r-full`} style={{ width: `${(p.s / total) * 100}%`, minWidth: 4 }} title={`${p.label}: ${duration(p.s)}`} />
          ))}
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
        {parts.map((p) => (
          <div key={p.key} className="flex items-center gap-2">
            <span aria-hidden className={`size-2.5 rounded-sm ${p.cls}`} />
            <dt className="text-fg-muted">{p.label}</dt>
            <dd className="ml-auto font-medium text-fg tabular-nums">{duration(p.s)}</dd>
          </div>
        ))}
        <div className="flex items-center gap-2">
          <span aria-hidden className="size-2.5" />
          <dt className="text-fg-muted">Wall clock</dt>
          <dd className="ml-auto font-medium text-fg tabular-nums">{duration(time.wall_s)}</dd>
        </div>
      </dl>
    </div>
  );
}

function BucketTable({ title, rows, nameLabel }: { title: string; rows: [string, CostBucket][]; nameLabel: string }) {
  return (
    <Card title={title} bodyClassName="overflow-x-auto">
      {rows.length === 0 ? (
        <EmptyState icon="cpu" title="No model calls" />
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-fg-subtle">
            <tr className="border-b border-line">
              <th scope="col" className="px-4 py-2 font-medium">{nameLabel}</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Calls</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Tokens in / out</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Avg latency</th>
              <th scope="col" className="px-4 py-2 text-right font-medium">Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map(([name, b]) => (
              <tr key={name}>
                <th scope="row" className="px-4 py-2 text-left font-medium">
                  <span className="font-mono text-[13px]">{name}</span>
                  {(b.errors > 0 || b.retries > 0) && (
                    <span className="ml-2 text-xs font-normal text-danger-fg">
                      {b.errors > 0 && `${b.errors} failed`}
                      {b.errors > 0 && b.retries > 0 && " · "}
                      {b.retries > 0 && `${b.retries} retries`}
                    </span>
                  )}
                </th>
                <td className="px-3 py-2 text-right tabular-nums">{b.calls}</td>
                <td className="px-3 py-2 text-right text-fg-muted tabular-nums">
                  {compact(b.input_tokens)} / {compact(b.output_tokens)}
                </td>
                <td className="px-3 py-2 text-right text-fg-muted tabular-nums">{b.calls ? `${Math.round(b.latency_ms / b.calls)} ms` : "—"}</td>
                <td className="px-4 py-2 text-right font-medium tabular-nums">{usd(b.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

const KIND_ICON: Record<TraceItem["kind"], IconName> = { llm: "cpu", tool: "wrench", event: "dot" };

function TraceRow({ item }: { item: TraceItem }) {
  const failed = item.ok === false && item.status !== "proposed";
  const label =
    item.kind === "llm"
      ? `${item.agent} · ${item.purpose}`
      : item.kind === "tool"
        ? `${item.agent} → ${item.tool}`
        : `${item.type}${item.node ? ` · ${item.node}` : ""}`;
  const detail = item.kind === "event" ? { payload: item.payload } : { input: item.input, output: item.output, error: item.error };
  return (
    <li>
      <details className="group">
        <summary className="grid cursor-pointer list-none grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 px-4 py-2 hover:bg-surface-2 sm:grid-cols-[72px_auto_minmax(0,1fr)_auto_auto_auto] [&::-webkit-details-marker]:hidden">
          <time dateTime={item.at} className="hidden font-mono text-[11px] text-fg-subtle tabular-nums sm:block">
            {clockTime(item.at)}
          </time>
          <span className={`grid size-6 place-items-center rounded-md ${failed ? "bg-danger-bg text-danger-fg" : item.kind === "event" ? "text-fg-subtle" : "bg-surface-2 text-fg-muted"}`}>
            <Icon name={failed ? "alert" : KIND_ICON[item.kind]} size={13} label={item.kind === "llm" ? "Model call" : item.kind === "tool" ? "Tool call" : "Event"} />
          </span>
          <span className={`truncate text-[13px] ${item.kind === "event" ? "text-fg-muted" : "text-fg"}`}>
            {label}
            {item.retry && (
              <Badge tone="warning" className="ml-2">
                retry {item.attempt}
              </Badge>
            )}
            {item.kind === "tool" && item.status && item.status !== "ok" && (
              <Badge tone={failed ? "danger" : "violet"} className="ml-2">
                {item.status}
              </Badge>
            )}
          </span>
          <span className="hidden text-right text-xs text-fg-subtle tabular-nums sm:block">
            {item.kind === "llm" ? `${compact(item.input_tokens ?? 0)}+${compact(item.output_tokens ?? 0)} tok` : ""}
          </span>
          <span className="hidden w-16 text-right text-xs text-fg-subtle tabular-nums sm:block">{item.latency_ms !== undefined ? `${item.latency_ms} ms` : ""}</span>
          <span className="w-16 text-right text-xs font-medium tabular-nums">{item.kind === "llm" ? usd(item.cost_usd) : ""}</span>
        </summary>
        <div className="border-t border-line bg-surface-2/40 px-4 py-3 sm:pl-[118px]">
          {item.model && <p className="mb-2 text-xs text-fg-subtle">model {item.model}</p>}
          <pre className="max-h-72 overflow-auto rounded-lg border border-line bg-surface p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap">
            {JSON.stringify(detail, null, 2)}
          </pre>
          <p className="mt-1.5 text-[11px] text-fg-subtle">Inputs and outputs are truncated; credentials are redacted on the server.</p>
        </div>
      </details>
    </li>
  );
}

export function TracePanel({ taskId, traceId }: { taskId: string; traceId: string }) {
  const [kind, setKind] = useState<"" | "llm" | "tool" | "event">("");
  const [agent, setAgent] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const cost = useQuery({
    queryKey: ["task", taskId, "cost"],
    queryFn: ({ signal }) => api<CostSummary>(`/tasks/${taskId}/cost`, { signal }),
  });
  const trace = useInfiniteQuery({
    queryKey: ["task", taskId, "trace", { kind, agent, errorsOnly }],
    queryFn: ({ pageParam, signal }) =>
      api<{ items: TraceItem[]; next_cursor: number | null }>(`/tasks/${taskId}/trace`, {
        signal,
        query: { cursor: pageParam, limit: 100, kind: kind || null, agent: agent || null, errors_only: errorsOnly || null },
      }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = trace.data?.pages.flatMap((p) => p.items) ?? [];
  const jaeger = import.meta.env.VITE_JAEGER_URL as string | undefined;

  if (cost.isError) return <ErrorState error={cost.error} onRetry={() => void cost.refetch()} />;
  const c = cost.data;
  const agents = Object.keys(c?.by_agent ?? {});

  return (
    <div className="space-y-4">
      {!c ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile label="Cost" value={usd(c.total.cost_usd)} sub={c.budget ? `of ${usd(c.budget.max_usd)} budget` : undefined} />
            <Tile
              label="Model calls"
              value={String(c.total.calls)}
              sub={c.total.errors || c.total.retries ? `${c.total.errors} failed · ${c.total.retries} retries` : "no failures or retries"}
            />
            <Tile label="Tokens" value={compact(c.total.input_tokens + c.total.output_tokens)} sub={`${compact(c.total.input_tokens)} in · ${compact(c.total.output_tokens)} out`} />
            <Tile label="Compute time" value={duration(c.time.compute_s)} sub={`human wait ${duration(c.time.human_wait_s)} (separate)`} />
          </div>
          <Card title="Where the time went">
            <TimeBar time={c.time} />
            {c.approvals.length > 0 && (
              <ul className="mt-4 space-y-1 border-t border-line pt-3 text-sm">
                {c.approvals.map((a) => (
                  <li key={a.key} className="flex items-center justify-between gap-3">
                    <span className="text-fg-muted">
                      Waited for <span className="text-fg">{a.kind.replace(/_/g, " ")}</span> ({a.status})
                    </span>
                    <span className="font-medium tabular-nums">{duration(a.wait_s)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <div className="grid gap-4 xl:grid-cols-2">
            <BucketTable title="By agent" nameLabel="Agent" rows={Object.entries(c.by_agent)} />
            <BucketTable title="By model" nameLabel="Model" rows={Object.entries(c.by_model)} />
          </div>
          {Object.keys(c.tools).length > 0 && (
            <Card title="Tools" bodyClassName="flex flex-wrap gap-2 p-4">
              {Object.entries(c.tools).map(([name, t]) => (
                <span key={name} className="inline-flex items-center gap-2 rounded-lg border border-line px-2.5 py-1 text-[13px]">
                  <span className="font-mono">{name}</span>
                  <span className="text-fg-subtle">×{t.calls}</span>
                  {t.errors > 0 && <span className="text-danger-fg">{t.errors} failed</span>}
                </span>
              ))}
            </Card>
          )}
        </>
      )}

      <Card
        title="Timeline"
        bodyClassName=""
        actions={
          jaeger && (
            <a href={`${jaeger.replace(/\/$/, "")}/trace/${traceId}`} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs font-medium text-accent-ink hover:underline">
              Open in Jaeger <Icon name="external" size={12} />
            </a>
          )
        }
      >
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
          <div role="radiogroup" aria-label="Show" className="flex rounded-lg border border-line bg-surface-2 p-0.5 text-xs">
            {(
              [
                ["", "All"],
                ["llm", "Model calls"],
                ["tool", "Tool calls"],
                ["event", "Events"],
              ] as const
            ).map(([v, l]) => (
              <button
                key={v}
                type="button"
                role="radio"
                aria-checked={kind === v}
                onClick={() => setKind(v)}
                className={`rounded-md px-2.5 py-1 font-medium ${kind === v ? "bg-surface text-fg shadow-card" : "text-fg-muted hover:text-fg"}`}
              >
                {l}
              </button>
            ))}
          </div>
          <label htmlFor="trace-agent" className="sr-only">
            Agent
          </label>
          <select id="trace-agent" value={agent} onChange={(e) => setAgent(e.target.value)} className={`${inputClass} h-8 w-40 py-1 text-xs`}>
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a}>{a}</option>
            ))}
          </select>
          <label className="inline-flex items-center gap-1.5 text-xs text-fg-muted">
            <input type="checkbox" checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)} className="size-3.5 accent-[var(--accent)]" />
            Errors and retries only
          </label>
        </div>
        {trace.isPending ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-8" />
            ))}
          </div>
        ) : trace.isError ? (
          <div className="p-4">
            <ErrorState error={trace.error} onRetry={() => void trace.refetch()} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon="list" title={errorsOnly ? "No errors or retries" : "Nothing recorded yet"} />
        ) : (
          <>
            <ol aria-label="Trace timeline" className="divide-y divide-line">
              {items.map((i) => (
                <TraceRow key={i.id} item={i} />
              ))}
            </ol>
            {trace.hasNextPage && (
              <div className="border-t border-line p-3 text-center">
                <Button size="sm" onClick={() => void trace.fetchNextPage()} busy={trace.isFetchingNextPage}>
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
