// In-browser stand-in for the API in the recorded demo (ADR-053). api() routes here when the
// app is built with VITE_DEMO=1; no request ever leaves the browser.
import { ApiError, type RequestOptions } from "../api/client";
import type { Approval, Page, TaskListItem } from "../api/types";
import * as pb from "./playback";
import { DEMO_TASK_ID, demo, recording } from "./store";

const LIVE_ONLY = "This needs the live backend. The recorded demo replays one finished run.";

function q(opts: RequestOptions, key: string): string | undefined {
  const v = opts.query?.[key];
  return v === null || v === undefined ? undefined : String(v);
}

function notFound(): never {
  throw new ApiError(404, "Not found in this recording.");
}

export async function demoApi<T>(path: string, opts: RequestOptions & { onHeaders?: (h: Headers) => void } = {}): Promise<T> {
  opts.onHeaders?.(new Headers());
  const method = opts.method ?? "GET";
  const p = path.replace(/^\//, "").split("?")[0]!.split("/");
  const v = demo.view();
  const out = (x: unknown) => Promise.resolve(x as T);

  if (p[0] === "me") {
    const who = recording.identities.find((i) => i.id === (opts.token ?? "")) ?? recording.identities[1]!;
    return out(who);
  }

  if (p[0] === "tasks") {
    if (p.length === 1 && method === "GET") {
      const page: Page<TaskListItem> = { items: [pb.listItem(v)], next_cursor: null };
      return out(page);
    }
    if (p.length === 1 || p[1] === "scenarios") throw new ApiError(403, LIVE_ONLY);
    if (p[1] !== DEMO_TASK_ID) notFound();
    switch (p[2]) {
      case undefined:
      case "detail":
        return out(pb.detail(v));
      case "trace": {
        const kinds = (opts.query?.kind as string[] | undefined) ?? null;
        const agent = q(opts, "agent");
        const errorsOnly = q(opts, "errors_only") === "true";
        const items = pb
          .trace(v)
          .filter((t) => !kinds || kinds.includes(t.kind))
          .filter((t) => !agent || t.agent === agent)
          .filter((t) => !errorsOnly || t.ok === false || t.status === "denied" || t.status === "failed");
        return out({ items, next_cursor: null });
      }
      case "cost":
        return out(pb.cost(v));
      default:
        throw new ApiError(403, LIVE_ONLY);
    }
  }

  if (p[0] === "approvals") {
    if (p.length === 1) {
      const st = q(opts, "state");
      return out(pb.approvals(v, st === "pending" ? "pending" : st === "decided" ? "decided" : "all"));
    }
    const a = pb.approvals(v).find((x) => x.id === p[1]) ?? notFound();
    if (p[2] === "decision" && method === "POST") return out(decide(a, opts.body));
    return out(a);
  }

  if (p[0] === "memories") {
    if (method === "DELETE") throw new ApiError(403, "Deleting memories is turned off in the recorded demo.");
    if (p[1] === "search") {
      const words = (q(opts, "q") ?? "").toLowerCase().split(/\W+/).filter((w) => w.length > 2);
      return out(recording.memories.filter((m) => words.some((w) => m.text.toLowerCase().includes(w))));
    }
    if (p[1]) return out(recording.memories.find((m) => m.id === p[1]) ?? notFound());
    return out(Number(q(opts, "offset") ?? 0) > 0 ? [] : recording.memories);
  }

  throw new ApiError(403, LIVE_ONLY);
}

const ACTION_LABEL: Record<string, string> = { approve: "Approve / Accept drafts", reject: "Reject", edit: "Edit", take_over: "Take over" };

function decide(a: Approval, body: unknown): Approval {
  if (a.status !== "pending") throw new ApiError(409, "Already decided.");
  const recorded = recording.approvals.find((x) => x.id === a.id)!;
  const want = String((recorded.decision as { action?: string } | null)?.action ?? "approve");
  const got = String(((body as { decision?: { action?: string } } | undefined)?.decision ?? {}).action ?? "");
  if (got !== want)
    throw new ApiError(
      422,
      `This is a recording of a real run, so only the decision made in that run can play back: ${ACTION_LABEL[want] ?? want}.`,
    );
  demo.decide(a.id);
  return { ...a, status: recorded.status, decision: recorded.decision };
}
