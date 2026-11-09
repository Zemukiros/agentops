// One decision form per approval kind. The server validates again (pydantic model + semantic
// checks, ADR-035) and the optimistic version check rejects stale decisions with 409; the form
// keeps the reviewer's input across a reload so nothing typed is lost.
import { useState, type FormEvent, type ReactNode } from "react";
import type { Approval } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Button, Field, inputClass } from "../../components/ui";
import { fieldsFromSchema, initialValues, toArgs, type FieldSpec, type FormValues } from "../../lib/schemaForm";
import { PlanEditor, planFromPayload, planToPayload } from "./PlanEditor";
import { hasErrors, validatePlan, type EditablePlan } from "../../lib/planEdit";

export type Decision = Record<string, unknown>;

type Action = "approve" | "edit" | "reject" | "take_over" | "confirmed_done" | "not_done";

const ACTIONS: Record<string, Action[]> = {
  approve_action: ["approve", "edit", "reject", "take_over"],
  approve_plan: ["approve", "edit", "reject", "take_over"],
  escalation: ["approve", "edit", "reject", "take_over"],
  final_review: ["approve", "edit", "reject", "take_over"],
  budget: ["approve", "reject"],
  recursion: ["approve", "reject", "take_over"],
  side_effect_review: ["confirmed_done", "not_done"],
};

const LABEL: Record<string, Partial<Record<Action, string>>> = {
  approve_action: { approve: "Approve", edit: "Edit & approve", reject: "Reject", take_over: "I did it myself" },
  approve_plan: { approve: "Approve plan", edit: "Edit plan", reject: "Reject", take_over: "Take over" },
  escalation: { approve: "Accept drafts", edit: "Edit drafts", reject: "Reject", take_over: "Take over" },
  final_review: { approve: "Accept", edit: "Edit deliverable", reject: "Reject", take_over: "Take over" },
  budget: { approve: "Raise limit & continue", reject: "Stop the run" },
  recursion: { approve: "Continue", reject: "Stop the run", take_over: "Take over" },
  side_effect_review: { confirmed_done: "It happened", not_done: "It did not happen" },
};

const HELP: Partial<Record<Action, string>> = {
  approve: "The run continues as proposed.",
  edit: "Your edits replace the proposal; they are validated again before anything runs.",
  reject: "Nothing is executed for this item.",
  take_over: "You handle it; the run records it as done by a human and does not execute it.",
  confirmed_done: "Records the side effect as done. Nothing is re-executed.",
  not_done: "Records the side effect as not done. Nothing is re-executed.",
};

export interface FormState {
  action: Action;
  note: string;
  args: FormValues;
  plan: EditablePlan | null;
  edits: Record<string, string>;
  deliverable: string;
  budget: { max_usd: string; max_llm_calls: string; max_tokens: string };
}

export function initialForm(a: Approval): FormState {
  const p = a.payload;
  const schema = (p.input_schema ?? null) as Record<string, unknown> | null;
  return {
    action: ACTIONS[a.kind]?.[0] ?? "approve",
    note: "",
    args: initialValues(fieldsFromSchema(schema), (p.args ?? {}) as Record<string, unknown>),
    plan: a.kind === "approve_plan" ? planFromPayload(p.plan) : null,
    edits: Object.fromEntries(
      ((p.subtasks ?? []) as { subtask_id: string; draft: string | null }[]).map((s) => [s.subtask_id, s.draft ?? ""]),
    ),
    deliverable: typeof p.deliverable === "string" ? p.deliverable : "",
    budget: {
      max_usd: typeof p.max_usd === "number" ? String(Math.round(p.max_usd * 2 * 100) / 100) : "",
      max_llm_calls: "",
      max_tokens: "",
    },
  };
}

/** Build the decision body for the API, or field errors. Pure (unit-tested). */
export function buildDecision(a: Approval, f: FormState): { decision?: Decision; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  const d: Decision = { action: f.action };
  if (f.note.trim()) d.note = f.note.trim();
  if (f.action === "edit") {
    if (a.kind === "approve_action") {
      const r = toArgs(fieldsFromSchema(a.payload.input_schema as Record<string, unknown>), f.args);
      Object.assign(errors, Object.fromEntries(Object.entries(r.errors).map(([k, v]) => [`args.${k}`, v])));
      d.args = r.args;
    } else if (a.kind === "approve_plan") {
      if (!f.plan || hasErrors(validatePlan(f.plan))) errors.plan = "Fix the plan errors first.";
      else d.plan = planToPayload(f.plan);
    } else if (a.kind === "escalation") {
      const edits = Object.fromEntries(Object.entries(f.edits).filter(([, v]) => v.trim()));
      if (!Object.keys(edits).length) errors.edits = "Provide at least one replacement draft.";
      d.edits = edits;
    } else if (a.kind === "final_review") {
      if (!f.deliverable.trim()) errors.deliverable = "Required";
      d.deliverable = f.deliverable;
    }
  }
  if (f.action === "take_over" && a.kind !== "approve_action") {
    if (!f.deliverable.trim()) errors.deliverable = "Write the deliverable you are providing.";
    d.deliverable = f.deliverable;
  }
  if (a.kind === "budget" && f.action === "approve") {
    const want = (k: keyof FormState["budget"], integer: boolean) => {
      const raw = f.budget[k].trim();
      if (!raw) return;
      const n = Number(raw);
      if (!Number.isFinite(n) || n <= 0 || (integer && !Number.isInteger(n))) errors[`budget.${k}`] = "Must be a positive number";
      else d[k] = n;
    };
    want("max_usd", false);
    want("max_llm_calls", true);
    want("max_tokens", true);
    if (!("max_usd" in d || "max_llm_calls" in d || "max_tokens" in d) && !errors["budget.max_usd"])
      errors["budget.max_usd"] = "Raise at least one limit.";
  }
  return Object.keys(errors).length ? { errors } : { decision: d, errors };
}

function SchemaField({ f, value, error, onChange }: { f: FieldSpec; value: string | boolean; error?: string; onChange: (v: string | boolean) => void }) {
  const id = `arg-${f.name}`;
  const common = {
    id,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? `${id}-error` : undefined,
  } as const;
  let control: ReactNode;
  if (f.kind === "boolean")
    control = <input {...common} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} className="size-4 accent-[var(--accent)]" />;
  else if (f.kind === "enum")
    control = (
      <select {...common} value={String(value)} onChange={(e) => onChange(e.target.value)} className={inputClass}>
        <option value="">—</option>
        {f.options?.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </select>
    );
  else if (f.kind === "text" || f.kind === "json" || f.kind === "list")
    control = (
      <textarea
        {...common}
        rows={f.kind === "text" ? 5 : 3}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
        className={`${inputClass} ${f.kind === "json" ? "font-mono text-xs" : ""}`}
      />
    );
  else
    control = (
      <input
        {...common}
        type={f.kind === "integer" || f.kind === "number" ? "number" : "text"}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
        className={inputClass}
      />
    );
  return (
    <Field
      label={`${f.label}${f.required ? "" : " (optional)"}`}
      htmlFor={id}
      error={error}
      hint={f.kind === "list" ? "One per line" : f.maxLength ? `Max ${f.maxLength} characters` : f.description}
    >
      {control}
    </Field>
  );
}

export function DecisionForm({
  approval,
  form,
  setForm,
  onSubmit,
  busy,
}: {
  approval: Approval;
  form: FormState;
  setForm: (f: FormState) => void;
  onSubmit: (d: Decision) => void;
  busy: boolean;
}) {
  const [errors, setErrors] = useState<Record<string, string>>({});
  const actions = ACTIONS[approval.kind] ?? ["approve", "reject"];
  const label = (x: Action) => LABEL[approval.kind]?.[x] ?? x;
  const set = (patch: Partial<FormState>) => setForm({ ...form, ...patch });

  function submit(e: FormEvent) {
    e.preventDefault();
    const r = buildDecision(approval, form);
    setErrors(r.errors);
    if (r.decision) onSubmit(r.decision);
  }

  const fields = fieldsFromSchema(approval.payload.input_schema as Record<string, unknown> | undefined);
  const needsDeliverable =
    (form.action === "take_over" && approval.kind !== "approve_action") ||
    (form.action === "edit" && approval.kind === "final_review");

  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <fieldset>
        <legend className="mb-2 text-sm font-semibold">Decision</legend>
        <div role="radiogroup" className="flex flex-wrap gap-2">
          {actions.map((x) => (
            <label
              key={x}
              className={`inline-flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-1.5 text-sm font-medium has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--focus)] ${
                form.action === x ? "border-accent bg-accent-soft text-accent-ink" : "border-line-strong text-fg-muted hover:bg-surface-2"
              }`}
            >
              <input
                type="radio"
                name="action"
                value={x}
                checked={form.action === x}
                onChange={() => {
                  setErrors({});
                  set({ action: x });
                }}
                className="sr-only"
              />
              {label(x)}
            </label>
          ))}
        </div>
        {HELP[form.action] && <p className="mt-2 text-xs text-fg-subtle">{HELP[form.action]}</p>}
      </fieldset>

      {form.action === "edit" && approval.kind === "approve_action" && (
        <div className="space-y-3 rounded-lg border border-line bg-surface-2/40 p-3">
          <p className="text-xs font-medium text-fg-subtle">
            Arguments for <span className="font-mono">{String(approval.payload.tool)}</span> (from the tool’s input schema)
          </p>
          {fields.length === 0 ? (
            <p className="text-sm text-fg-muted">This tool has no editable schema; approve or reject it.</p>
          ) : (
            fields.map((f) => (
              <SchemaField
                key={f.name}
                f={f}
                value={form.args[f.name] ?? ""}
                error={errors[`args.${f.name}`]}
                onChange={(v) => set({ args: { ...form.args, [f.name]: v } })}
              />
            ))
          )}
        </div>
      )}

      {form.action === "edit" && approval.kind === "approve_plan" && form.plan && (
        <div className="space-y-2">
          <PlanEditor plan={form.plan} onChange={(plan) => set({ plan })} />
          {errors.plan && <p className="text-xs font-medium text-danger-fg">{errors.plan}</p>}
        </div>
      )}

      {form.action === "edit" && approval.kind === "escalation" && (
        <div className="space-y-3">
          {Object.keys(form.edits).map((sid) => (
            <Field key={sid} label={`Replacement for ${sid}`} htmlFor={`edit-${sid}`}>
              <textarea
                id={`edit-${sid}`}
                rows={6}
                value={form.edits[sid]}
                onChange={(e) => set({ edits: { ...form.edits, [sid]: e.target.value } })}
                className={inputClass}
              />
            </Field>
          ))}
          {errors.edits && <p className="text-xs font-medium text-danger-fg">{errors.edits}</p>}
        </div>
      )}

      {needsDeliverable && (
        <Field label="Deliverable" htmlFor="deliverable" error={errors.deliverable} hint="Markdown. This becomes the task’s result.">
          <textarea
            id="deliverable"
            rows={8}
            value={form.deliverable}
            onChange={(e) => set({ deliverable: e.target.value })}
            aria-invalid={errors.deliverable ? true : undefined}
            aria-describedby={errors.deliverable ? "deliverable-error" : "deliverable-hint"}
            className={inputClass}
          />
        </Field>
      )}

      {approval.kind === "budget" && form.action === "approve" && (
        <div className="grid gap-3 sm:grid-cols-3">
          {(
            [
              ["max_usd", "New max cost (USD)"],
              ["max_llm_calls", "New max model calls"],
              ["max_tokens", "New max tokens"],
            ] as const
          ).map(([k, l]) => (
            <Field key={k} label={l} htmlFor={`budget-${k}`} error={errors[`budget.${k}`]}>
              <input
                id={`budget-${k}`}
                type="number"
                min="0"
                value={form.budget[k]}
                onChange={(e) => set({ budget: { ...form.budget, [k]: e.target.value } })}
                aria-invalid={errors[`budget.${k}`] ? true : undefined}
                className={inputClass}
                placeholder="unchanged"
              />
            </Field>
          ))}
        </div>
      )}

      <Field label="Note (optional)" htmlFor="note" hint="Kept with the decision and shown in the task’s history.">
        <textarea id="note" rows={2} maxLength={2000} value={form.note} onChange={(e) => set({ note: e.target.value })} className={inputClass} />
      </Field>

      <div className="flex items-center justify-end gap-2">
        {Object.keys(errors).length > 0 && (
          <p className="mr-auto flex items-center gap-1 text-xs font-medium text-danger-fg" role="alert">
            <Icon name="alert" size={14} /> Fix the highlighted fields.
          </p>
        )}
        <Button
          type="submit"
          variant={form.action === "reject" || form.action === "not_done" ? "danger" : "primary"}
          busy={busy}
        >
          {label(form.action)}
        </Button>
      </div>
    </form>
  );
}
