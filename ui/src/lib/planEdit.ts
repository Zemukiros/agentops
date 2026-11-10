// Plan editor model + validation. Mirrors the server's checks (core/scheduler.validate_plan):
// unique slug ids, known dependencies, no self-dependency, no cycles, 1..6 subtasks, a known
// specialist. The server re-validates on submit.

export const SPECIALISTS = ["research", "analysis", "writing", "code"] as const;
export const COMPLEXITY = ["low", "medium", "high"] as const;
export const MAX_SUBTASKS = 6;
const ID = /^[a-z0-9][a-z0-9_-]{0,31}$/;

export interface EditableSubtask {
  id: string;
  description: string;
  specialist: string;
  depends_on: string[];
  expected_output: string;
  complexity: string;
}

export interface EditablePlan {
  subtasks: EditableSubtask[];
  confidence: number;
  rationale: string;
}

export interface PlanErrors {
  plan: string[];
  rows: Record<number, Partial<Record<keyof EditableSubtask, string>>>;
}

export function validatePlan(p: EditablePlan): PlanErrors {
  const errors: PlanErrors = { plan: [], rows: {} };
  const rowErr = (i: number, k: keyof EditableSubtask, msg: string) => {
    (errors.rows[i] ??= {})[k] = msg;
  };
  if (p.subtasks.length === 0) errors.plan.push("A plan needs at least one subtask.");
  if (p.subtasks.length > MAX_SUBTASKS) errors.plan.push(`At most ${MAX_SUBTASKS} subtasks.`);
  const seen = new Map<string, number>();
  p.subtasks.forEach((s, i) => {
    if (!ID.test(s.id)) rowErr(i, "id", "Lowercase letters, digits, - or _ (max 32)");
    else if (seen.has(s.id)) rowErr(i, "id", "Duplicate id");
    seen.set(s.id, i);
    if (!s.description.trim()) rowErr(i, "description", "Required");
    if (!s.expected_output.trim()) rowErr(i, "expected_output", "Required");
    if (!(SPECIALISTS as readonly string[]).includes(s.specialist)) rowErr(i, "specialist", "Unknown specialist");
    const unknown = s.depends_on.filter((d) => !p.subtasks.some((o) => o.id === d));
    if (s.depends_on.includes(s.id)) rowErr(i, "depends_on", "Can’t depend on itself");
    else if (unknown.length) rowErr(i, "depends_on", `Unknown: ${unknown.join(", ")}`);
  });
  const cycle = findCycle(p.subtasks);
  if (cycle) errors.plan.push(`Dependency cycle: ${cycle.join(" → ")}`);
  return errors;
}

export function hasErrors(e: PlanErrors): boolean {
  return e.plan.length > 0 || Object.keys(e.rows).length > 0;
}

/** A dependency cycle as a path (first node repeated at the end), or null. */
export function findCycle(subtasks: EditableSubtask[]): string[] | null {
  const deps = new Map<string, string[]>();
  for (const s of subtasks) if (!deps.has(s.id)) deps.set(s.id, s.depends_on); // duplicates: first wins
  const state = new Map<string, 1 | 2>();
  const stack: string[] = [];
  const visit = (id: string): string[] | null => {
    if (state.get(id) === 2) return null;
    if (state.get(id) === 1) return [...stack.slice(stack.indexOf(id)), id];
    state.set(id, 1);
    stack.push(id);
    for (const d of deps.get(id) ?? []) {
      if (!deps.has(d) || d === id) continue;
      const c = visit(d);
      if (c) return c;
    }
    stack.pop();
    state.set(id, 2);
    return null;
  };
  for (const s of subtasks) {
    const c = visit(s.id);
    if (c) return c;
  }
  return null;
}

/** Rename a subtask and keep everyone's dependencies pointing at it. */
export function renameSubtask(p: EditablePlan, index: number, id: string): EditablePlan {
  const old = p.subtasks[index]?.id;
  return {
    ...p,
    subtasks: p.subtasks.map((s, i) =>
      i === index ? { ...s, id } : { ...s, depends_on: s.depends_on.map((d) => (d === old ? id : d)) },
    ),
  };
}

/** Remove a subtask and drop dependencies on it. */
export function removeSubtask(p: EditablePlan, index: number): EditablePlan {
  const gone = p.subtasks[index]?.id;
  return {
    ...p,
    subtasks: p.subtasks
      .filter((_, i) => i !== index)
      .map((s) => ({ ...s, depends_on: s.depends_on.filter((d) => d !== gone) })),
  };
}

export function newSubtask(p: EditablePlan): EditableSubtask {
  let n = p.subtasks.length + 1;
  while (p.subtasks.some((s) => s.id === `step-${n}`)) n++;
  return { id: `step-${n}`, description: "", specialist: "writing", depends_on: [], expected_output: "", complexity: "low" };
}
