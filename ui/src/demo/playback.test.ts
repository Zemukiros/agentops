import { describe, expect, it } from "vitest";
import recordingJson from "./recording.json";
import * as pb from "./playback";

const rec = recordingJson as unknown as pb.Recording;
const N = rec.events.length;
const START = Date.parse("2030-01-01T00:00:00Z");
const at = (revealed: number) => pb.view(rec, revealed, START);
const firstIndex = (type: string) => rec.events.findIndex((e) => e.type === type);
const gates = rec.events.map((_, i) => pb.gateAt(rec.events, i)).filter(Boolean);

describe("recording", () => {
  it("is one finished real-model run with two human decisions", () => {
    expect(rec.events.at(-1)!.type).toBe("task.finished");
    expect(rec.detail.status).toBe("completed");
    expect(gates).toEqual(rec.approvals.map((a) => a.id).sort((a, b) => firstIndexOf(a) - firstIndexOf(b)));
    expect(rec.events.filter((e) => e.type === "llm.call").every((e) => !String(e.payload.model).startsWith("fake"))).toBe(true);
  });

  it("contains no credentials or local links", () => {
    const s = JSON.stringify(rec);
    expect(s).not.toMatch(/sk-ant|bearer\s|localhost|127\.0\.0\.1/i);
  });
});

function firstIndexOf(approvalId: string): number {
  return rec.events.findIndex((e) => e.type === "approval.decided" && e.payload.approval_id === approvalId);
}

describe("pacing", () => {
  it("starts immediately and caps every gap", () => {
    const d = pb.delays(rec.events);
    expect(d[0]).toBe(0);
    expect(Math.max(...d)).toBeLessThanOrEqual(pb.MAX_GAP_MS);
    expect(d.reduce((a, b) => a + b, 0)).toBeLessThan(90_000);
  });

  it("shifts timestamps so the run starts at playback start", () => {
    expect(pb.events(at(1))[0]!.created_at).toBe(new Date(START).toISOString());
  });
});

describe("snapshots", () => {
  it("follow the task through its states", () => {
    expect(pb.status(at(0))).toBe("queued");
    expect(pb.status(at(firstIndex("plan.created") + 1))).toBe("running");
    const gate = rec.events.findIndex((e) => e.type === "approval.decided");
    expect(pb.status(at(gate))).toBe("awaiting_approval");
    expect(pb.status(at(N))).toBe("completed");
  });

  it("hide what hasn't happened yet", () => {
    const early = pb.detail(at(firstIndex("plan.created")));
    expect(early.plan).toBeNull();
    expect(early.final).toBeNull();
    expect(early.actions).toEqual([]);
    expect(early.subtasks.every((s) => s.output === null && s.reviews.length === 0)).toBe(true);
    const beforeReview = pb.detail(at(firstIndex("final_review.verdict")));
    expect(beforeReview.final).toBeNull();
  });

  it("show a pending approval at each gate, decided afterwards", () => {
    const gate = rec.events.findIndex((e) => e.type === "approval.decided");
    const v = at(gate);
    const pending = pb.approvals(v, "pending");
    expect(pending).toHaveLength(1);
    expect(pending[0]!.decision).toBeNull();
    expect(pb.listItem(v).pending_approvals).toBe(1);
    expect(pb.approvals(at(gate + 1), "pending")).toHaveLength(0);
    expect(pb.approvals(at(gate + 1), "decided")[0]!.status).toBe("approved");
  });

  it("end exactly at the recorded result", () => {
    const d = pb.detail(at(N));
    expect(d.final).toBe(rec.detail.final);
    expect(d.subtasks).toEqual(rec.detail.subtasks);
    expect(d.actions).toEqual(rec.detail.actions);
    expect(d.budget).toEqual(rec.detail.budget);
    expect(pb.cost(at(N))).toEqual(rec.cost);
    expect(pb.trace(at(N))).toHaveLength(rec.trace.length);
  });

  it("aggregate cost from the revealed calls the same way the API did", () => {
    const c = pb.cost(at(N - 1)); // every model call revealed, task not finished yet
    expect(c.total.calls).toBe(rec.cost.total.calls);
    expect(c.total.cost_usd).toBeCloseTo(rec.cost.total.cost_usd, 6);
    for (const [agent, b] of Object.entries(rec.cost.by_agent)) {
      expect(c.by_agent[agent]!.calls).toBe(b.calls);
      expect(c.by_agent[agent]!.cost_usd).toBeCloseTo(b.cost_usd, 6);
    }
    expect(Object.keys(c.by_model).sort()).toEqual(Object.keys(rec.cost.by_model).sort());
  });

  it("show this run's memories only once it has written them", () => {
    const written = firstIndex("memory.consolidated");
    expect(pb.memories(at(written))).toEqual([]);
    const after = pb.memories(at(written + 1));
    expect(after.length).toBeGreaterThan(0);
    expect(after.every((m) => m.source_task_id === rec.task.id)).toBe(true);
  });

  it("never show more than the run did, at any point", () => {
    let prevCost = 0;
    for (let i = 0; i <= N; i++) {
      const v = at(i);
      const d = pb.detail(v);
      d.subtasks.forEach((s, j) => {
        expect(s.revisions).toBeLessThanOrEqual(rec.detail.subtasks[j]!.revisions);
        expect(s.reviews.length).toBeLessThanOrEqual(rec.detail.subtasks[j]!.reviews.length);
      });
      const c = pb.cost(v).total.cost_usd;
      expect(c).toBeGreaterThanOrEqual(prevCost - 1e-9);
      expect(c).toBeLessThanOrEqual(rec.cost.total.cost_usd + 1e-9);
      prevCost = c;
      const t = pb.cost(v).tools;
      for (const [name, b] of Object.entries(t)) expect(b.errors).toBeLessThanOrEqual(rec.cost.tools[name]!.errors);
    }
  });
});
