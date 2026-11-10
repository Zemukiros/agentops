import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { api } from "../api/client";
import type { CompareSide, Comparison } from "../api/types";
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/Layout";
import { Markdown } from "../components/Markdown";
import { Badge, StatusBadge } from "../components/StatusBadge";
import { Card, EmptyState, ErrorState, Skeleton } from "../components/ui";
import { diffStats, parseUnifiedDiff } from "../lib/diff";
import { duration, shortId, usd } from "../lib/format";

function SideHeader({ s }: { s: CompareSide }) {
  return (
    <div className="rounded-xl border border-line bg-surface p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={s.kind === "replay" ? "violet" : "neutral"} icon={s.kind === "replay" ? "branch" : "list"}>
          {s.kind === "replay" ? "branch" : "original"}
        </Badge>
        <Link to={`/tasks/${s.id}`} className="font-mono text-sm text-accent-ink hover:underline">
          {shortId(s.id)}
        </Link>
        <span className="ml-auto">
          <StatusBadge status={s.status} />
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-3 gap-2 text-sm">
        {(
          [
            ["Cost", usd(s.cost.cost_usd)],
            ["Model calls", String(s.cost.calls)],
            ["Compute", duration(s.time.compute_s)],
          ] as const
        ).map(([k, v]) => (
          <div key={k}>
            <dt className="text-xs text-fg-subtle">{k}</dt>
            <dd className="font-medium tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>
      {s.explanation && <p className="mt-2 text-xs text-warning-fg">{s.explanation}</p>}
    </div>
  );
}

export function ComparePage() {
  const { a, b } = useParams();
  const q = useQuery({ queryKey: ["compare", a, b], queryFn: () => api<Comparison>(`/tasks/${a}/compare/${b}`) });
  if (q.isPending)
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/2" />
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
        </div>
      </div>
    );
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const { left, right, final_diff, subtask_changes } = q.data;
  const rows = parseUnifiedDiff(final_diff);
  const stats = diffStats(rows);

  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-xs text-fg-subtle">
        <Link to={`/tasks/${left.id}?tab=replay`} className="hover:text-fg hover:underline">
          {shortId(left.id)} replay
        </Link>
        <span aria-hidden> / </span>compare
      </nav>
      <PageHeader title="Compare branches" description={<span className="line-clamp-2">{left.request}</span>} />
      <div className="grid gap-4 md:grid-cols-2">
        <SideHeader s={left} />
        <SideHeader s={right} />
      </div>

      <Card id="diff" title="Deliverable diff" actions={rows.length > 0 && <span className="text-xs text-fg-muted">+{stats.added} −{stats.removed} lines</span>} bodyClassName="">
        {rows.length === 0 ? (
          <EmptyState icon="check" title="Same deliverable on both branches" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mono text-[12.5px] leading-6">
              <caption className="sr-only">Unified diff of the deliverables: removed lines are from the left task, added lines from the right</caption>
              <tbody>
                {rows.map((r, i) =>
                  r.kind === "file" ? null : r.kind === "hunk" ? (
                    <tr key={i} className="bg-surface-2 text-fg-subtle">
                      <td colSpan={3} className="px-4 py-0.5">
                        {r.text}
                      </td>
                    </tr>
                  ) : (
                    <tr key={i} className={r.kind === "add" ? "bg-diff-add" : r.kind === "del" ? "bg-diff-del" : ""}>
                      <td className="w-10 px-2 text-right text-fg-subtle select-none">{r.left ?? ""}</td>
                      <td className="w-10 px-2 text-right text-fg-subtle select-none">{r.right ?? ""}</td>
                      <td className="px-3 break-words whitespace-pre-wrap text-fg">
                        <span aria-hidden className={`mr-2 select-none ${r.kind === "add" ? "text-success-fg" : r.kind === "del" ? "text-danger-fg" : "text-fg-subtle"}`}>
                          {r.kind === "add" ? "+" : r.kind === "del" ? "−" : " "}
                        </span>
                        <span className="sr-only">{r.kind === "add" ? "added: " : r.kind === "del" ? "removed: " : ""}</span>
                        {r.text}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Subtasks" bodyClassName="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-fg-subtle">
            <tr className="border-b border-line">
              <th scope="col" className="px-4 py-2 font-medium">Subtask</th>
              <th scope="col" className="px-3 py-2 font-medium">Left</th>
              <th scope="col" className="px-3 py-2 font-medium">Right</th>
              <th scope="col" className="px-4 py-2 font-medium">Output</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {subtask_changes.map((s) => (
              <tr key={s.id}>
                <th scope="row" className="px-4 py-2 text-left font-mono text-[13px] font-semibold">{s.id}</th>
                <td className="px-3 py-2">{s.left ? <StatusBadge status={s.left} kind="subtask" /> : "—"}</td>
                <td className="px-3 py-2">{s.right ? <StatusBadge status={s.right} kind="subtask" /> : "—"}</td>
                <td className="px-4 py-2 text-xs">
                  {s.output_changed ? <Badge tone="warning">changed</Badge> : <span className="text-fg-subtle">same</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        {[left, right].map((s) => (
          <Card key={s.id} title={`${s.kind === "replay" ? "Branch" : "Original"} ${shortId(s.id)}: actions and deliverable`}>
            {s.actions.length > 0 && (
              <ul className="mb-4 space-y-1.5 text-sm">
                {s.actions.map((a, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-2">
                    <Icon name={a.tool === "send_email" ? "mail" : "wrench"} size={14} />
                    <span className="font-mono text-[13px]">{a.tool}</span>
                    {a.status && <StatusBadge status={a.status} kind="outcome" />}
                    {a.detail && <span className="text-xs text-fg-muted">{a.detail}</span>}
                  </li>
                ))}
              </ul>
            )}
            {s.final ? <Markdown>{s.final}</Markdown> : <p className="text-sm text-fg-subtle">No deliverable.</p>}
          </Card>
        ))}
      </div>
    </div>
  );
}
