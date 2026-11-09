import { describe, expect, it } from "vitest";
import type { TaskEvent } from "../api/types";
import { duration, feedLine, relativeTime, usd } from "./format";

const ev = (type: string, payload: Record<string, unknown> = {}, node: string | null = null): TaskEvent => ({
  id: 1,
  type,
  node,
  payload,
  created_at: "2026-11-07T12:00:00Z",
});

describe("formatters", () => {
  it("formats money without false precision", () => {
    expect(usd(0)).toBe("$0");
    expect(usd(0.04136)).toBe("$0.0414");
    expect(usd(1.5)).toBe("$1.5");
    expect(usd(0.00001)).toBe("<$0.0001");
    expect(usd(null)).toBe("—");
  });

  it("formats durations at a readable unit", () => {
    expect(duration(0.25)).toBe("250 ms");
    expect(duration(4.26)).toBe("4.3 s");
    expect(duration(75)).toBe("1 min 15 s");
    expect(duration(3720)).toBe("1 h 2 min");
  });

  it("formats relative times", () => {
    const now = Date.parse("2026-11-07T12:10:00Z");
    expect(relativeTime("2026-11-07T12:09:50Z", now)).toBe("just now");
    expect(relativeTime("2026-11-07T12:00:00Z", now)).toBe("10 min ago");
    expect(relativeTime("2026-11-08T12:10:00Z", now)).toBe("in 24 h");
    expect(relativeTime("2026-11-07T12:40:00Z", now)).toBe("in 30 min");
  });
});

describe("feedLine", () => {
  it("describes the events that matter, with a tone", () => {
    expect(feedLine(ev("review.verdict", { subtask_id: "brief", verdict: "revise", score: 3 }))).toMatchObject({
      text: "brief: review revise (3/5)",
      tone: "warning",
    });
    expect(feedLine(ev("tool.call", { agent: "writing", tool: "send_email", status: "proposed" }))).toMatchObject({
      text: "writing → send_email (proposed)",
      tone: "violet",
    });
    expect(feedLine(ev("task.finished", { status: "completed_partial" }))).toMatchObject({ tone: "warning" });
    expect(feedLine(ev("approval.requested", { kind: "approve_action" }))?.text).toBe("Approval needed: approve action");
  });

  it("marks model calls as detail (hidden by default) unless they failed", () => {
    expect(feedLine(ev("llm.call", { role: "writing", ok: true }))?.detail).toBe(true);
    expect(feedLine(ev("llm.call", { role: "writing", ok: false }))).toMatchObject({ detail: false, tone: "danger" });
  });

  it("skips noise", () => {
    expect(feedLine(ev("schedule.decided"))).toBeNull();
    expect(feedLine(ev("node.started", {}, "intake"))).toBeNull();
    expect(feedLine(ev("task.status", { status: "running" }))).toBeNull();
  });
});
