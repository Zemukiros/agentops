import { describe, expect, it } from "vitest";
import type { SubtaskView, TaskEvent } from "../api/types";
import { layout, liveSubtasks, mergeStatus, planFromEvents, type PlanNode } from "./dag";

let n = 0;
const ev = (type: string, payload: Record<string, unknown> = {}, node: string | null = null): TaskEvent => ({
  id: ++n,
  type,
  node,
  payload,
  created_at: "2026-11-07T12:00:00Z",
});

const plan: PlanNode[] = [
  { id: "research", description: "r", specialist: "research", depends_on: [] },
  { id: "brief", description: "b", specialist: "writing", depends_on: ["research"] },
];

describe("liveSubtasks", () => {
  it("follows a subtask through dispatch, work, tools, a revise loop and acceptance", () => {
    const events = [
      ev("subtask.dispatched", { subtask_id: "brief", wave: 2 }),
      ev("node.started", { subtask_id: "brief" }, "specialist"),
      ev("tool.call", { subtask_id: "brief", tool: "send_email", status: "proposed" }),
    ];
    let s = liveSubtasks(events).brief!;
    expect([s.status, s.wave, s.toolCalls, s.lastTool]).toEqual(["working", 2, 1, "send_email"]);

    events.push(ev("node.completed", { subtask_id: "brief" }, "specialist"));
    expect(liveSubtasks(events).brief!.status).toBe("in_review");
    events.push(ev("review.verdict", { subtask_id: "brief", verdict: "revise", score: 3, revision: 0 }));
    s = liveSubtasks(events).brief!;
    expect([s.status, s.score, s.verdict]).toEqual(["revising", 3, "revise"]);

    events.push(ev("node.started", { subtask_id: "brief" }, "specialist"));
    events.push(ev("review.verdict", { subtask_id: "brief", verdict: "accept", score: 4, revision: 1 }));
    events.push(ev("subtask.finished", { subtask_id: "brief", status: "accepted" }));
    s = liveSubtasks(events).brief!;
    expect([s.status, s.score, s.revision]).toEqual(["accepted", 4, 1]);
  });

  it("never moves a finished subtask back to working on late events", () => {
    const s = liveSubtasks([
      ev("subtask.finished", { subtask_id: "a", status: "failed" }),
      ev("node.started", { subtask_id: "a" }, "specialist"),
      ev("review.verdict", { subtask_id: "a", verdict: "revise", score: 2 }),
    ]).a!;
    expect(s.status).toBe("failed");
  });

  it("ignores events without a subtask and other nodes' starts", () => {
    const out = liveSubtasks([ev("llm.call", { role: "supervisor" }), ev("node.started", { subtask_id: "x" }, "synthesize")]);
    expect(out.x?.status).toBe("pending");
    expect(Object.keys(out)).toEqual(["x"]);
  });

  it("marks blocked subtasks", () => {
    expect(liveSubtasks([ev("subtask.blocked", { subtask_id: "s", reason: "dep failed" })]).s!.status).toBe("blocked");
  });
});

describe("mergeStatus", () => {
  const snap = (status: string) => ({ status }) as SubtaskView;
  it("prefers a terminal snapshot, then the live stream, then the snapshot", () => {
    const live = liveSubtasks([ev("node.started", { subtask_id: "a" }, "specialist")]).a;
    expect(mergeStatus(snap("accepted"), live)).toBe("accepted");
    expect(mergeStatus(snap("pending"), live)).toBe("working");
    expect(mergeStatus(snap("pending"), undefined)).toBe("pending");
    expect(mergeStatus(undefined, undefined)).toBe("pending");
  });
});

describe("planFromEvents", () => {
  it("uses the newest plan (a re-plan replaces the first)", () => {
    const first = ev("plan.created", { plan: { subtasks: [plan[0]] } });
    const second = ev("plan.created", { plan: { subtasks: plan } });
    expect(planFromEvents([first, second])?.map((p) => p.id)).toEqual(["research", "brief"]);
    expect(planFromEvents([ev("task.created")])).toBeNull();
  });
});

describe("layout", () => {
  it("places dependencies left of dependents and stacks siblings", () => {
    const nodes: PlanNode[] = [
      { id: "a", description: "", specialist: "writing", depends_on: [] },
      { id: "b", description: "", specialist: "writing", depends_on: [] },
      { id: "c", description: "", specialist: "writing", depends_on: ["a", "b"] },
      { id: "d", description: "", specialist: "writing", depends_on: ["c", "a"] },
    ];
    const by = Object.fromEntries(layout(nodes).map((p) => [p.id, p]));
    expect([by.a!.level, by.b!.level, by.c!.level, by.d!.level]).toEqual([0, 0, 1, 2]);
    expect([by.a!.row, by.b!.row]).toEqual([0, 1]);
  });

  it("survives unknown dependencies and cycles without looping", () => {
    const nodes: PlanNode[] = [
      { id: "x", description: "", specialist: "writing", depends_on: ["ghost", "y"] },
      { id: "y", description: "", specialist: "writing", depends_on: ["x"] },
    ];
    expect(layout(nodes)).toHaveLength(2);
  });
});

describe("live header and rows", () => {
  it("takes the newest status event", async () => {
    const { liveTaskStatus } = await import("./dag");
    expect(liveTaskStatus([ev("task.status", { status: "running" }), ev("llm.call"), ev("task.status", { status: "awaiting_approval" })])).toBe("awaiting_approval");
    expect(liveTaskStatus([ev("task.created")])).toBeNull();
  });
  it("shows planned subtasks before the snapshot has them", async () => {
    const { subtaskRows } = await import("./dag");
    const rows = subtaskRows(plan, [{ id: "research", status: "accepted" } as SubtaskView]);
    expect(rows.map((r) => [r.id, r.status])).toEqual([["research", "accepted"], ["brief", "pending"]]);
  });
});
