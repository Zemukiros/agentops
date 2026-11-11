// The demo's clock (ADR-053): reveals the recorded events one by one at the recorded pace,
// stops before each recorded human decision until the visitor makes it, and can restart.
// State lives in memory only; a reload starts the run over.
import { useSyncExternalStore } from "react";
import recordingJson from "./recording.json";
import { delays, gateAt, view, type Recording, type View } from "./playback";

export const recording = recordingJson as unknown as Recording;
export const DEMO_TASK_ID = recording.task.id;

const DELAYS = delays(recording.events);
const TOTAL = recording.events.length;

interface State {
  revealed: number;
  startMs: number;
  /** approval id the run is waiting on, if any */
  waitingFor: string | null;
  decided: ReadonlySet<string>;
  started: boolean;
}

let state: State = { revealed: 0, startMs: Date.now(), waitingFor: null, decided: new Set(), started: false };
let timer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<() => void>();

function set(next: Partial<State>): void {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

function step(): void {
  timer = undefined;
  if (state.revealed >= TOTAL) return;
  const gate = gateAt(recording.events, state.revealed);
  if (gate && !state.decided.has(gate)) {
    set({ waitingFor: gate });
    return;
  }
  set({ revealed: state.revealed + 1, waitingFor: null });
  schedule();
}

function schedule(): void {
  if (timer || state.revealed >= TOTAL) return;
  timer = setTimeout(step, DELAYS[state.revealed] ?? 0);
}

export const demo = {
  get: (): State => state,
  view: (): View => view(recording, state.revealed, state.startMs),
  total: TOTAL,
  /** Start the run once; later calls are no-ops. */
  start(): void {
    if (state.started) return;
    set({ started: true, startMs: Date.now() });
    schedule();
  },
  restart(): void {
    if (timer) clearTimeout(timer);
    timer = undefined;
    set({ revealed: 0, startMs: Date.now(), waitingFor: null, decided: new Set(), started: true });
    schedule();
  },
  /** The visitor made the recorded decision: continue the run. */
  decide(approvalId: string): void {
    set({ decided: new Set([...state.decided, approvalId]) });
    if (state.waitingFor === approvalId) {
      // reveal the decision itself right away so the inbox updates on click
      if (timer) clearTimeout(timer);
      step();
    }
  },
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

export function useDemo(): State {
  return useSyncExternalStore(demo.subscribe, demo.get, demo.get);
}
