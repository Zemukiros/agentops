// Unified diff (from the server's difflib) -> typed rows for rendering.
export type DiffRow =
  | { kind: "file"; text: string }
  | { kind: "hunk"; text: string }
  | { kind: "add" | "del" | "ctx"; text: string; left: number | null; right: number | null };

export function parseUnifiedDiff(lines: string[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let l = 0;
  let r = 0;
  for (const line of lines) {
    if (line.startsWith("---") || line.startsWith("+++")) {
      rows.push({ kind: "file", text: line });
      continue;
    }
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (h) {
      l = Number(h[1]);
      r = Number(h[2]);
      rows.push({ kind: "hunk", text: line });
      continue;
    }
    if (line.startsWith("+")) rows.push({ kind: "add", text: line.slice(1), left: null, right: r++ });
    else if (line.startsWith("-")) rows.push({ kind: "del", text: line.slice(1), left: l++, right: null });
    else rows.push({ kind: "ctx", text: line.slice(1), left: l++, right: r++ });
  }
  return rows;
}

export function diffStats(rows: DiffRow[]): { added: number; removed: number } {
  return {
    added: rows.filter((x) => x.kind === "add").length,
    removed: rows.filter((x) => x.kind === "del").length,
  };
}

/** Human description of a fork point. */
export function nodeLabel(next: string): string {
  return (
    {
      plan: "Before planning",
      plan_gate: "Plan made, before plan approval",
      schedule: "Between waves",
      action_gate: "Subtasks done, before action approval",
      synthesize: "Before writing the deliverable",
      final_review: "Deliverable written, before final review",
      subtask_worker: "Inside a parallel wave",
      execute_actions: "Executing approved actions",
      consolidate: "Saving memories",
      recall: "Recalling memories",
      intake: "Intake",
      __start__: "Start",
    }[next] ?? next
  );
}
