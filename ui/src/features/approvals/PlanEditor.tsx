// Structured plan editor: subtask rows (id, specialist, dependencies, description, expected
// output, complexity) with inline validation and a live DAG preview.
import { useMemo } from "react";
import { Icon } from "../../components/Icon";
import { Button, inputClass } from "../../components/ui";
import {
  COMPLEXITY,
  MAX_SUBTASKS,
  SPECIALISTS,
  newSubtask,
  removeSubtask,
  renameSubtask,
  validatePlan,
  type EditablePlan,
  type EditableSubtask,
} from "../../lib/planEdit";
import { PlanDag } from "../task/PlanDag";

export function planFromPayload(raw: unknown): EditablePlan {
  const p = (raw ?? {}) as Partial<EditablePlan>;
  return {
    confidence: typeof p.confidence === "number" ? p.confidence : 0.5,
    rationale: typeof p.rationale === "string" ? p.rationale : "",
    subtasks: (p.subtasks ?? []).map((s) => ({
      id: s.id,
      description: s.description ?? "",
      specialist: s.specialist ?? "writing",
      depends_on: [...(s.depends_on ?? [])],
      expected_output: s.expected_output ?? "",
      complexity: s.complexity ?? "medium",
    })),
  };
}

export function planToPayload(p: EditablePlan): Record<string, unknown> {
  return { confidence: p.confidence, rationale: p.rationale || "edited by a reviewer", subtasks: p.subtasks };
}

export function PlanEditor({ plan, onChange }: { plan: EditablePlan; onChange: (p: EditablePlan) => void }) {
  const errors = useMemo(() => validatePlan(plan), [plan]);
  const update = (i: number, patch: Partial<EditableSubtask>) =>
    onChange({ ...plan, subtasks: plan.subtasks.map((s, j) => (j === i ? { ...s, ...patch } : s)) });

  return (
    <div className="space-y-3">
      <PlanDag plan={plan.subtasks} snapshot={[]} live={{}} />
      {errors.plan.length > 0 && (
        <ul role="alert" className="space-y-1 rounded-lg border border-danger-line bg-danger-bg px-3 py-2 text-xs font-medium text-danger-fg">
          {errors.plan.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      <ol className="space-y-3">
        {plan.subtasks.map((s, i) => {
          const e = errors.rows[i] ?? {};
          const others = plan.subtasks.filter((_, j) => j !== i);
          const fid = (k: string) => `plan-${i}-${k}`;
          return (
            <li key={i} className="rounded-lg border border-line bg-surface p-3">
              <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_160px_120px_auto]">
                <div>
                  <label htmlFor={fid("id")} className="mb-1 block text-xs font-medium text-fg-muted">
                    Id
                  </label>
                  <input
                    id={fid("id")}
                    value={s.id}
                    onChange={(ev) => onChange(renameSubtask(plan, i, ev.target.value))}
                    aria-invalid={e.id ? true : undefined}
                    aria-describedby={e.id ? `${fid("id")}-error` : undefined}
                    className={`${inputClass} font-mono`}
                  />
                  {e.id && (
                    <p id={`${fid("id")}-error`} className="mt-1 text-xs text-danger-fg">
                      {e.id}
                    </p>
                  )}
                </div>
                <div>
                  <label htmlFor={fid("spec")} className="mb-1 block text-xs font-medium text-fg-muted">
                    Specialist
                  </label>
                  <select id={fid("spec")} value={s.specialist} onChange={(ev) => update(i, { specialist: ev.target.value })} className={inputClass}>
                    {SPECIALISTS.map((x) => (
                      <option key={x}>{x}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor={fid("cx")} className="mb-1 block text-xs font-medium text-fg-muted">
                    Complexity
                  </label>
                  <select id={fid("cx")} value={s.complexity} onChange={(ev) => update(i, { complexity: ev.target.value })} className={inputClass}>
                    {COMPLEXITY.map((x) => (
                      <option key={x}>{x}</option>
                    ))}
                  </select>
                </div>
                <div className="flex items-end">
                  <button
                    type="button"
                    onClick={() => onChange(removeSubtask(plan, i))}
                    aria-label={`Remove subtask ${s.id}`}
                    className="grid size-9 place-items-center rounded-lg text-fg-subtle hover:bg-danger-bg hover:text-danger-fg"
                  >
                    <Icon name="trash" />
                  </button>
                </div>
              </div>
              <fieldset className="mt-3">
                <legend className="mb-1 text-xs font-medium text-fg-muted">Depends on</legend>
                {others.length === 0 ? (
                  <p className="text-xs text-fg-subtle">No other subtasks.</p>
                ) : (
                  <div className="flex flex-wrap gap-x-4 gap-y-1">
                    {others.map((o) => (
                      <label key={o.id + i} className="inline-flex items-center gap-1.5 text-sm">
                        <input
                          type="checkbox"
                          checked={s.depends_on.includes(o.id)}
                          onChange={(ev) =>
                            update(i, {
                              depends_on: ev.target.checked ? [...s.depends_on, o.id] : s.depends_on.filter((d) => d !== o.id),
                            })
                          }
                          className="size-3.5 accent-[var(--accent)]"
                        />
                        <span className="font-mono text-xs">{o.id || "(no id)"}</span>
                      </label>
                    ))}
                  </div>
                )}
                {e.depends_on && <p className="mt-1 text-xs text-danger-fg">{e.depends_on}</p>}
              </fieldset>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                {(
                  [
                    ["description", "Description"],
                    ["expected_output", "Expected output"],
                  ] as const
                ).map(([k, l]) => (
                  <div key={k}>
                    <label htmlFor={fid(k)} className="mb-1 block text-xs font-medium text-fg-muted">
                      {l}
                    </label>
                    <textarea
                      id={fid(k)}
                      rows={2}
                      value={s[k]}
                      onChange={(ev) => update(i, { [k]: ev.target.value })}
                      aria-invalid={e[k] ? true : undefined}
                      className={inputClass}
                    />
                    {e[k] && <p className="mt-1 text-xs text-danger-fg">{e[k]}</p>}
                  </div>
                ))}
              </div>
            </li>
          );
        })}
      </ol>
      <Button
        type="button"
        size="sm"
        icon="plus"
        disabled={plan.subtasks.length >= MAX_SUBTASKS}
        onClick={() => onChange({ ...plan, subtasks: [...plan.subtasks, newSubtask(plan)] })}
      >
        Add subtask
      </Button>
    </div>
  );
}
