// Live task events over SSE (ADR-041). EventSource can't send an Authorization header, and a
// bearer token must never go in a URL, so each connection uses a fresh single-use ticket.
// A used ticket can't reconnect, so on any error we close, fetch a new ticket and reopen from
// the last event id we saw (the server replays everything after it; ids dedupe overlap).
import { useEffect, useReducer, useRef, useState } from "react";
import { api, buildUrl } from "./client";
import type { TaskEvent } from "./types";

export type StreamState = "connecting" | "live" | "reconnecting" | "closed" | "error";

interface Ticket {
  ticket: string;
  expires_at: string;
}

export function mergeEvents(current: TaskEvent[], incoming: TaskEvent[]): TaskEvent[] {
  if (incoming.length === 0) return current;
  const seen = new Set(current.map((e) => e.id));
  const fresh = incoming.filter((e) => !seen.has(e.id));
  if (fresh.length === 0) return current;
  return [...current, ...fresh].sort((a, b) => a.id - b.id);
}

export const MAX_RETRY_DELAY_MS = 15_000;
export function retryDelay(attempt: number): number {
  return Math.min(MAX_RETRY_DELAY_MS, 500 * 2 ** Math.min(attempt, 5));
}

function useLiveTaskEvents(taskId: string | undefined): {
  events: TaskEvent[];
  state: StreamState;
} {
  const [events, add] = useReducer(mergeEvents, []);
  const [state, setState] = useState<StreamState>("connecting");
  const lastId = useRef(0);

  useEffect(() => {
    if (!taskId) return;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;
    lastId.current = 0;

    const connect = async (): Promise<void> => {
      if (stopped) return;
      setState(attempt === 0 ? "connecting" : "reconnecting");
      let t: Ticket;
      try {
        t = await api<Ticket>(`/tasks/${taskId}/stream-ticket`, { method: "POST" });
      } catch {
        schedule();
        return;
      }
      if (stopped) return;
      const url = buildUrl(`/tasks/${taskId}/events`, { ticket: t.ticket, after: lastId.current });
      source = new EventSource(url);
      source.onopen = () => {
        attempt = 0;
        setState("live");
      };
      source.onmessage = (msg: MessageEvent<string>) => {
        const ev = JSON.parse(msg.data) as TaskEvent;
        lastId.current = Math.max(lastId.current, ev.id);
        add([ev]);
        if (ev.type === "task.finished") {
          stopped = true;
          source?.close();
          setState("closed");
        }
      };
      source.onerror = () => {
        source?.close();
        if (!stopped) schedule();
      };
    };
    const schedule = (): void => {
      setState("reconnecting");
      timer = setTimeout(() => void connect(), retryDelay(attempt++));
    };

    void connect();
    return () => {
      stopped = true;
      source?.close();
      if (timer) clearTimeout(timer);
    };
  }, [taskId]);

  return { events, state };
}

/** Recorded demo (ADR-053): the same events, revealed by the demo clock instead of SSE. The
 * dynamic import keeps the recording out of normal builds. */
function useDemoTaskEvents(taskId: string | undefined): { events: TaskEvent[]; state: StreamState } {
  const [snap, setSnap] = useState<{ events: TaskEvent[]; state: StreamState }>({ events: [], state: "connecting" });
  useEffect(() => {
    if (!taskId) return;
    let off: (() => void) | undefined;
    let alive = true;
    void Promise.all([import("../demo/store"), import("../demo/playback")]).then(([{ demo }, pb]) => {
      if (!alive) return;
      const update = () => {
        const v = demo.view();
        const events = pb.events(v);
        setSnap({ events, state: events.at(-1)?.type === "task.finished" ? "closed" : "live" });
      };
      off = demo.subscribe(update);
      demo.start();
      update();
    });
    return () => {
      alive = false;
      off?.();
    };
  }, [taskId]);
  return snap;
}

export const useTaskEvents = import.meta.env.VITE_DEMO === "1" ? useDemoTaskEvents : useLiveTaskEvents;
