// Theme: "system" (default) follows prefers-color-scheme; "light"/"dark" pin it.
// public/theme-init.js applies the stored choice before first paint.
import { useEffect, useState, useSyncExternalStore } from "react";

export type ThemeChoice = "system" | "light" | "dark";
const KEY = "agentops.theme";

function read(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    /* not persisted */
  }
}

export function useTheme(): [ThemeChoice, (t: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(read);
  useEffect(() => applyTheme(choice), [choice]);
  return [choice, setChoice];
}

const media = (): MediaQueryList | null =>
  typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

/** The theme actually showing (for components like React Flow that need it explicitly). */
export function useResolvedTheme(): "light" | "dark" {
  return useSyncExternalStore(
    (cb) => {
      const m = media();
      m?.addEventListener("change", cb);
      const obs = new MutationObserver(cb);
      obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
      return () => {
        m?.removeEventListener("change", cb);
        obs.disconnect();
      };
    },
    () => {
      const t = document.documentElement.dataset.theme;
      if (t === "light" || t === "dark") return t;
      return media()?.matches ? "dark" : "light";
    },
    () => "light",
  );
}
