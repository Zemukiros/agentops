import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, buildUrl, errorMessage } from "./client";
import { MAX_RETRY_DELAY_MS, mergeEvents, retryDelay } from "./events";
import { session } from "./session";
import type { TaskEvent } from "./types";

const e = (id: number): TaskEvent => ({ id, type: "x", node: null, payload: {}, created_at: "" });

describe("mergeEvents", () => {
  it("dedupes by id and keeps id order (a reconnect replays overlapping events)", () => {
    const merged = mergeEvents([e(1), e(2), e(4)], [e(4), e(3), e(5)]);
    expect(merged.map((x) => x.id)).toEqual([1, 2, 3, 4, 5]);
  });
  it("returns the same array when nothing is new (no re-render)", () => {
    const cur = [e(1)];
    expect(mergeEvents(cur, [e(1)])).toBe(cur);
  });
});

describe("retryDelay", () => {
  it("backs off exponentially and is capped", () => {
    expect(retryDelay(0)).toBe(500);
    expect(retryDelay(1)).toBe(1000);
    expect(retryDelay(50)).toBe(MAX_RETRY_DELAY_MS);
  });
});

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    session.clear();
  });

  it("builds URLs with repeated params and drops empty ones", () => {
    const u = new URL(buildUrl("/tasks", { status: ["failed", "rejected"], q: "", cursor: null, limit: 5 }));
    expect(u.pathname).toBe("/tasks");
    expect(u.searchParams.getAll("status")).toEqual(["failed", "rejected"]);
    expect(u.searchParams.has("q")).toBe(false);
    expect(u.searchParams.get("limit")).toBe("5");
  });

  it("turns FastAPI validation errors into readable text", () => {
    expect(errorMessage([{ loc: ["body", "budget", "max_usd"], msg: "must be > 0" }])).toBe("budget.max_usd: must be > 0");
    expect(errorMessage("version conflict")).toBe("version conflict");
  });

  it("sends the active bearer token and surfaces 409 as a conflict", async () => {
    session.add({ id: "u1", name: "demo-user", role: "user", token: "tok-123" });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: "approval changed" }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);
    const err = await api("/approvals/x/decision", { method: "POST", body: {} }).catch((x: unknown) => x);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).isConflict).toBe(true);
    expect((err as ApiError).message).toBe("approval changed");
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-123");
  });

  it("never puts the token in the URL", async () => {
    session.add({ id: "u1", name: "demo-user", role: "user", token: "tok-secret" });
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api("/tasks", { query: { q: "x" } });
    expect(String((fetchMock.mock.calls[0] as unknown as [string])[0])).not.toContain("tok-secret");
  });
});

describe("session", () => {
  afterEach(() => session.clear());
  it("keeps several identities, switches, and falls back when the active one is removed", () => {
    session.add({ id: "u", name: "demo-user", role: "user", token: "a" });
    session.add({ id: "r", name: "demo-reviewer", role: "reviewer", token: "b" });
    expect(session.active()?.id).toBe("r");
    session.switchTo("u");
    expect(session.token()).toBe("a");
    session.remove("u");
    expect(session.active()?.id).toBe("r");
    expect(JSON.parse(sessionStorage.getItem("agentops.session")!).identities).toHaveLength(1);
    expect(localStorage.length).toBe(0); // never persisted beyond the tab
  });
});
