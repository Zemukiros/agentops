import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Approval, TaskDetail } from "../api/types";
import { demoApi } from "./server";
import { DEMO_TASK_ID, demo, recording } from "./store";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

async function untilWaiting(): Promise<string> {
  for (let i = 0; i < 500 && !demo.get().waitingFor; i++) await vi.advanceTimersByTimeAsync(4000);
  return demo.get().waitingFor!;
}

describe("demo API", () => {
  it("plays the run, pauses for each recorded decision, and finishes", async () => {
    demo.restart();
    const first = await untilWaiting();
    const pending = await demoApi<Approval[]>("/approvals", { query: { state: "pending" } });
    expect(pending.map((a) => a.id)).toEqual([first]);

    // a different decision than the recorded one can't play back
    await expect(
      demoApi(`/approvals/${first}/decision`, { method: "POST", token: "demo-reviewer", body: { version: 1, decision: { action: "reject" } } }),
    ).rejects.toMatchObject({ status: 422 });
    expect(demo.get().waitingFor).toBe(first);

    // only reviewers can decide, as in the real API
    await expect(
      demoApi(`/approvals/${first}/decision`, { method: "POST", token: "demo-user", body: { version: 1, decision: { action: "approve" } } }),
    ).rejects.toMatchObject({ status: 403 });
    const decided = await demoApi<Approval>(`/approvals/${first}/decision`, {
      method: "POST",
      token: "demo-reviewer",
      body: { version: 1, decision: { action: "approve" } },
    });
    expect(decided).toMatchObject({ status: "approved", decided_by: "demo-reviewer" });
    expect(decided.decided_at).not.toBeNull();
    expect((await demoApi<Approval>(`/approvals/${first}`)).status).toBe("approved");

    const second = await untilWaiting();
    expect(second).not.toBe(first);
    await demoApi(`/approvals/${second}/decision`, { method: "POST", token: "demo-reviewer", body: { version: 1, decision: { action: "approve" } } });
    await vi.advanceTimersByTimeAsync(120_000);

    const d = await demoApi<TaskDetail>(`/tasks/${DEMO_TASK_ID}/detail`);
    expect(d.status).toBe("completed");
    expect(d.final).toBe(recording.detail.final);
  });

  it("refuses what needs the live backend, without any network call", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    await expect(demoApi("/tasks", { method: "POST", body: { request: "x" } })).rejects.toMatchObject({ status: 403 });
    await expect(demoApi(`/memories/${recording.memories[0]!.id}`, { method: "DELETE" })).rejects.toMatchObject({ status: 403 });
    await expect(demoApi(`/tasks/${DEMO_TASK_ID}/checkpoints`)).rejects.toMatchObject({ status: 403 });
    await expect(demoApi("/memories/search", { query: { q: "pgvector" } })).rejects.toMatchObject({ status: 403 });
    await expect(demoApi("/tasks/00000000-0000-0000-0000-000000000000/detail")).rejects.toMatchObject({ status: 404 });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("restarts cleanly while paused at a decision", async () => {
    demo.restart();
    const first = await untilWaiting();
    demo.restart();
    expect(demo.get()).toMatchObject({ revealed: 0, waitingFor: null });
    expect((await demoApi<Approval[]>("/approvals", { query: { state: "pending" } })).length).toBe(0);
    expect(await untilWaiting()).toBe(first);
  });

  it("filters the task list like the API", async () => {
    demo.restart();
    const page = (status: string) => demoApi<{ items: unknown[] }>("/tasks", { query: { status } });
    expect((await page("failed")).items).toHaveLength(0);
    expect((await demoApi<{ items: unknown[] }>("/tasks", { query: { q: "pgvector" } })).items).toHaveLength(1);
    expect((await demoApi<{ items: unknown[] }>("/tasks", { query: { q: "kubernetes" } })).items).toHaveLength(0);
  });

  it("answers /me for both demo identities", async () => {
    expect(await demoApi("/me", { token: "demo-user" })).toMatchObject({ role: "user" });
    expect(await demoApi("/me", { token: "demo-reviewer" })).toMatchObject({ role: "reviewer" });
    await expect(demoApi("/me", { token: null })).rejects.toMatchObject({ status: 401 });
  });
});
