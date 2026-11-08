// Signed-in identities. Tokens live in sessionStorage only (cleared with the tab), never in
// localStorage, cookies or URLs. Several identities can be kept for quick switching
// (e.g. the seeded user and reviewer); exactly one is active.
import { useSyncExternalStore } from "react";
import type { Me } from "./types";

export interface Identity extends Me {
  token: string;
}

interface SessionState {
  identities: Identity[];
  activeId: string | null;
}

const KEY = "agentops.session";
let state: SessionState = load();
const listeners = new Set<() => void>();

function load(): SessionState {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as SessionState;
      if (Array.isArray(parsed.identities)) return parsed;
    }
  } catch {
    /* storage unavailable or corrupt: start signed out */
  }
  return { identities: [], activeId: null };
}

function save(next: SessionState): void {
  state = next;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* keep in memory only */
  }
  listeners.forEach((l) => l());
}

export const session = {
  get: (): SessionState => state,
  active: (): Identity | null => state.identities.find((i) => i.id === state.activeId) ?? null,
  token: (): string | null => session.active()?.token ?? null,
  add(identity: Identity): void {
    const others = state.identities.filter((i) => i.id !== identity.id);
    save({ identities: [...others, identity], activeId: identity.id });
  },
  switchTo(id: string): void {
    if (state.identities.some((i) => i.id === id)) save({ ...state, activeId: id });
  },
  remove(id: string): void {
    const identities = state.identities.filter((i) => i.id !== id);
    save({
      identities,
      activeId: state.activeId === id ? (identities[0]?.id ?? null) : state.activeId,
    });
  },
  clear(): void {
    save({ identities: [], activeId: null });
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

export function useSession(): SessionState & { active: Identity | null } {
  const s = useSyncExternalStore(session.subscribe, session.get, session.get);
  return { ...s, active: s.identities.find((i) => i.id === s.activeId) ?? null };
}
