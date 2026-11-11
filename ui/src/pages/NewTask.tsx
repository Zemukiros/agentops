import { useMutation, useQuery } from "@tanstack/react-query";
import { Suspense, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { api } from "../api/client";
import type { Task } from "../api/types";
import { PageHeader } from "../components/Layout";
import { Button, Card, Field, inputClass } from "../components/ui";
import { DemoUnavailable } from "../demo/lazy";

const MAX = 20_000;

interface BudgetForm {
  max_usd: string;
  max_llm_calls: string;
  max_tokens: string;
}

/** Validate optional budget fields; empty = use the server default. */
export function parseBudget(f: BudgetForm): { value: Record<string, number> | null; errors: Partial<BudgetForm> } {
  const errors: Partial<BudgetForm> = {};
  const out: Record<string, number> = {};
  const num = (k: keyof BudgetForm, integer: boolean) => {
    const raw = f[k].trim();
    if (!raw) return;
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) errors[k] = "Must be a positive number";
    else if (integer && !Number.isInteger(n)) errors[k] = "Must be a whole number";
    else out[k] = n;
  };
  num("max_usd", false);
  num("max_llm_calls", true);
  num("max_tokens", true);
  return { value: Object.keys(out).length ? out : null, errors };
}

function NewTaskForm() {
  const [request, setRequest] = useState("");
  // null = not chosen yet: the server's default (ui_demo in fake mode) applies
  const [picked, setScenario] = useState<string | null>(null);
  const [budget, setBudget] = useState<BudgetForm>({ max_usd: "", max_llm_calls: "", max_tokens: "" });
  const [errors, setErrors] = useState<Partial<BudgetForm> & { request?: string }>({});
  const navigate = useNavigate();
  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: () =>
      api<{ fake_mode: boolean; default: string | null; scenarios: { name: string; description: string }[] }>(
        "/tasks/scenarios",
      ),
    staleTime: Infinity,
  });
  const scenario = picked ?? scenarios.data?.default ?? "";
  const chosen = scenarios.data?.scenarios.find((s) => s.name === scenario);
  const create = useMutation({
    mutationFn: (body: object) => api<Task>("/tasks", { method: "POST", body }),
    onSuccess: (t) => navigate(`/tasks/${t.id}`),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const b = parseBudget(budget);
    const next: typeof errors = { ...b.errors };
    if (!request.trim()) next.request = "Describe what you want done.";
    setErrors(next);
    if (Object.keys(next).length) return;
    create.mutate({
      request: request.trim(),
      ...(scenario ? { scenario } : {}),
      ...(b.value ? { budget: b.value } : {}),
    });
  }

  const budgetField = (k: keyof BudgetForm, label: string, hint: string, step: string) => (
    <Field label={label} htmlFor={k} hint={hint} error={errors[k]}>
      <input
        id={k}
        inputMode="decimal"
        type="number"
        min="0"
        step={step}
        value={budget[k]}
        onChange={(e) => setBudget((b) => ({ ...b, [k]: e.target.value }))}
        aria-invalid={errors[k] ? true : undefined}
        aria-describedby={errors[k] ? `${k}-error` : `${k}-hint`}
        className={inputClass}
        placeholder="default"
      />
    </Field>
  );

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="New task"
        description="Describe the outcome. A supervisor plans it, specialists work on it, a reviewer checks each step, and anything that leaves the system waits for approval."
      />
      <form onSubmit={submit} noValidate className="space-y-4">
        <Card>
          <Field
            label="Request"
            htmlFor="request"
            error={errors.request}
            hint={`${request.length.toLocaleString()} / ${MAX.toLocaleString()} characters`}
          >
            <textarea
              id="request"
              rows={6}
              maxLength={MAX}
              value={request}
              onChange={(e) => setRequest(e.target.value)}
              aria-invalid={errors.request ? true : undefined}
              aria-describedby={errors.request ? "request-error" : "request-hint"}
              className={`${inputClass} min-h-36 resize-y leading-relaxed`}
              placeholder="e.g. Compare pgvector with dedicated vector databases for our scale, write a one-page brief and email it to the team."
            />
          </Field>
          {scenarios.data?.fake_mode && (
            <div className="mt-4">
              <Field
                label="Demo scenario (fake mode)"
                htmlFor="scenario"
                hint={chosen?.description ?? "Scripted model responses for local runs. Real mode ignores this."}
              >
                <select id="scenario" value={scenario} onChange={(e) => setScenario(e.target.value)} className={inputClass}>
                  {scenarios.data.scenarios.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}: {s.description}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          )}
        </Card>

        <Card>
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-fg">
              Budget <span className="font-normal text-fg-subtle">(optional)</span>
            </summary>
            <p className="mt-1 text-sm text-fg-muted">
              Hard limits for this task. At 80% you get a warning; at 100% the run pauses and a reviewer can raise the
              limit or stop it.
            </p>
            <div className="mt-4 grid gap-4 sm:grid-cols-3">
              {budgetField("max_usd", "Max cost (USD)", "e.g. 0.50", "0.01")}
              {budgetField("max_llm_calls", "Max model calls", "e.g. 40", "1")}
              {budgetField("max_tokens", "Max tokens", "e.g. 150000", "1000")}
            </div>
          </details>
        </Card>

        {create.isError && (
          <p role="alert" className="rounded-lg border border-danger-line bg-danger-bg px-3 py-2 text-sm text-danger-fg">
            {create.error.message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => navigate("/tasks")}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" icon="spark" busy={create.isPending}>
            Start task
          </Button>
        </div>
      </form>
    </div>
  );
}

export function NewTask() {
  if (DemoUnavailable)
    return (
      <Suspense fallback={null}>
        <DemoUnavailable what="Starting a new task" />
      </Suspense>
    );
  return <NewTaskForm />;
}
