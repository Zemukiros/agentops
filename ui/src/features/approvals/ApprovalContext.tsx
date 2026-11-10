// What the reviewer needs to see before deciding, per approval kind.
import type { Approval, Verdict } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { Badge } from "../../components/StatusBadge";
import { Callout } from "../../components/ui";
import { usd } from "../../lib/format";
import { PlanDag } from "../task/PlanDag";
import { planFromPayload } from "./PlanEditor";

export const KIND_LABEL: Record<string, string> = {
  approve_action: "Action",
  approve_plan: "Plan",
  escalation: "Escalation",
  final_review: "Final review",
  budget: "Budget limit",
  recursion: "Step limit",
  side_effect_review: "Side-effect check",
};

function Args({ args, order = [] }: { args: Record<string, unknown>; order?: string[] }) {
  const rank = (k: string) => (order.includes(k) ? order.indexOf(k) : order.length);
  const entries = Object.entries(args).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 rounded-lg border border-line bg-surface-2/50 px-3 py-2.5 text-[13px]">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="font-mono text-fg-subtle">{k}</dt>
          <dd className="break-words whitespace-pre-wrap text-fg">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ReviewLine({ r }: { r: Verdict }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <Badge tone={r.verdict === "accept" ? "success" : r.verdict === "revise" ? "warning" : "danger"}>{r.verdict}</Badge>
      <span className="font-medium text-fg-muted">{r.score}/5</span>
      {r.feedback_for_specialist && <span className="text-fg">{r.feedback_for_specialist}</span>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold tracking-wide text-fg-subtle uppercase">{title}</h3>
      {children}
    </section>
  );
}

export function ApprovalContext({ approval: a, editing = false }: { approval: Approval; editing?: boolean }) {
  const p = a.payload;
  switch (a.kind) {
    case "approve_action":
      return (
        <div className="space-y-4">
          <Section title="Proposed action">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono font-semibold">{String(p.tool)}</span>
              <Badge tone={p.risk === "destructive" ? "danger" : p.risk === "external" ? "warning" : "info"}>{String(p.risk)}</Badge>
              <span className="text-xs text-fg-subtle">
                by {String(p.agent)} in {String(p.subtask_id)}
              </span>
            </div>
            <Args
              args={(p.args ?? {}) as Record<string, unknown>}
              order={((p.input_schema as { required?: string[] } | undefined)?.required ?? []) as string[]}
            />
          </Section>
          {typeof p.rationale === "string" && p.rationale && (
            <Section title="Why the agent proposed it">
              <p className="text-sm text-fg">{p.rationale}</p>
            </Section>
          )}
        </div>
      );
    case "approve_plan": {
      const plan = planFromPayload(p.plan);
      const c = (p.confidence ?? {}) as Record<string, number>;
      return (
        <div className="space-y-4">
          <Callout tone="warning" icon="alert" title={`Plan confidence ${c.effective?.toFixed(2)} is below ${c.threshold?.toFixed(2)}`}>
            Self-reported {c.self_reported?.toFixed(2)}, structural {c.structural?.toFixed(2)}. Approve it, edit it, or take
            over.
          </Callout>
          {!editing && <PlanDag plan={plan.subtasks} snapshot={[]} live={{}} />}
          <ul className="space-y-1.5 text-[13px]">
            {plan.subtasks.map((s) => (
              <li key={s.id}>
                <span className="font-mono font-semibold">{s.id}</span> <span className="text-fg-subtle">({s.specialist}, {s.complexity})</span>{" "}
                {s.description}
              </li>
            ))}
          </ul>
        </div>
      );
    }
    case "escalation":
      return (
        <div className="space-y-4">
          {((p.subtasks ?? []) as { subtask_id: string; reason: string | null; draft: string | null; reviews: Verdict[] }[]).map((s) => (
            <Section key={s.subtask_id} title={`${s.subtask_id}${s.reason ? `: ${s.reason}` : ""}`}>
              <div className="space-y-2">
                {s.draft ? (
                  <div className="rounded-lg border border-line bg-surface-2/50 p-3">
                    <Markdown>{s.draft}</Markdown>
                  </div>
                ) : (
                  <p className="text-sm text-fg-subtle">No draft.</p>
                )}
                {s.reviews.map((r, i) => (
                  <ReviewLine key={i} r={r} />
                ))}
              </div>
            </Section>
          ))}
        </div>
      );
    case "final_review": {
      const r = p.review as Verdict | null;
      return (
        <div className="space-y-4">
          {r && (
            <Section title="Final review">
              <ReviewLine r={r} />
              {r.issues.length > 0 && (
                <ul className="mt-1.5 list-disc pl-5 text-sm text-fg-muted">
                  {r.issues.map((x, i) => (
                    <li key={i}>{x}</li>
                  ))}
                </ul>
              )}
            </Section>
          )}
          <Section title={p.partial ? "Deliverable (partial)" : "Deliverable"}>
            <div className="rounded-lg border border-line bg-surface-2/50 p-3">
              <Markdown>{String(p.deliverable ?? "")}</Markdown>
            </div>
          </Section>
        </div>
      );
    }
    case "budget":
      return (
        <Callout tone="warning" icon="coins" title="The task hit its budget">
          {String(p.reason ?? "")}. Used {usd(p.used_usd as number)} of {usd(p.max_usd as number)}; {String(p.llm_calls)} of{" "}
          {String(p.max_llm_calls)} model calls. Raising the limit resumes from the last checkpoint.
        </Callout>
      );
    case "recursion":
      return (
        <Callout tone="warning" icon="alert" title="The run hit its step limit">
          {String(p.reason ?? "")}. Continue (from the last checkpoint), take over with your own deliverable, or stop.
        </Callout>
      );
    case "side_effect_review":
      return (
        <div className="space-y-4">
          <Callout tone="danger" icon="alert" title="Outcome unknown">
            {String(p.question ?? "")}
          </Callout>
          <Section title={String(p.tool)}>
            <Args args={(p.args ?? {}) as Record<string, unknown>} />
          </Section>
          <p className="text-xs text-fg-subtle">
            Idempotency key <span className="font-mono">{String(p.idempotency_key).slice(0, 16)}…</span>. If it was an email,
            search Mailpit for this Message-ID.
          </p>
        </div>
      );
    default:
      return <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 text-xs">{JSON.stringify(p, null, 2)}</pre>;
  }
}
