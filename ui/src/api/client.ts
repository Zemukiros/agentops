// Thin fetch wrapper: bearer auth from the active identity, JSON in/out, typed errors.
import { session } from "./session";

export const API_BASE: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
  /** A version conflict: someone else changed the resource since it was loaded. */
  get isConflict(): boolean {
    return this.status === 409;
  }
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | string[] | null | undefined>;
}

export function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(path.replace(/^\//, ""), API_BASE.endsWith("/") ? API_BASE : `${API_BASE}/`);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => url.searchParams.append(k, x));
    else url.searchParams.set(k, String(v));
  }
  return url.toString();
}

/** FastAPI errors: {"detail": "..."} or {"detail": [{loc, msg, ...}]} -> readable text. */
export function errorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const item = d as { loc?: unknown[]; msg?: string };
        const where = (item.loc ?? []).filter((x) => x !== "body").join(".");
        return where ? `${where}: ${item.msg ?? "invalid"}` : (item.msg ?? "invalid");
      })
      .join("; ");
  }
  return "Something went wrong";
}

/** Like api(), plus the response headers (for header-based pagination). */
export async function apiWithHeaders<T>(path: string, opts: RequestOptions = {}): Promise<{ data: T; headers: Headers }> {
  let headers = new Headers();
  const data = await api<T>(path, { ...opts, onHeaders: (h) => (headers = h) });
  return { data, headers };
}

export async function api<T>(path: string, opts: RequestOptions & { onHeaders?: (h: Headers) => void } = {}): Promise<T> {
  if (import.meta.env.VITE_DEMO === "1") {
    // recorded demo: answered in the browser; the branch and its chunk vanish from normal builds
    const { demoApi } = await import("../demo/server");
    return demoApi<T>(path, opts);
  }
  const token = opts.token === undefined ? session.token() : opts.token;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(buildUrl(path, opts.query), {
    method: opts.method ?? "GET",
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal,
  });
  opts.onHeaders?.(res.headers);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | undefined)?.detail ?? data;
    throw new ApiError(res.status, errorMessage(detail));
  }
  return data as T;
}
