// Entry points for the recorded demo (ADR-053). With VITE_DEMO unset these are all null and
// the demo code and recording are dropped from the bundle.
import { lazy } from "react";

export const DEMO = import.meta.env.VITE_DEMO === "1";

export const DemoBanner = DEMO ? lazy(() => import("./ui").then((m) => ({ default: m.DemoBanner }))) : null;
export const DemoEntry = DEMO ? lazy(() => import("./ui").then((m) => ({ default: m.DemoEntry }))) : null;
export const DemoUnavailable = DEMO ? lazy(() => import("./ui").then((m) => ({ default: m.DemoUnavailable }))) : null;
