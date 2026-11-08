// Screenshot capture against the running compose stack (fake mode). Everything shown comes
// from the real API: tasks are created through it, approvals are decided through it.
// Usage: node scripts/capture.mjs <outDir> [ui=http://localhost:5173] [api=http://localhost:8000]
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const out = resolve(process.argv[2] ?? "../docs/screenshots");
const UI = process.argv[3] ?? "http://localhost:5173";
const API = process.argv[4] ?? "http://localhost:8000";
mkdirSync(out, { recursive: true });

const tokens = Object.fromEntries(
  readFileSync(resolve("../.agentops-tokens"), "utf8")
    .split("\n")
    .filter((l) => l && !l.startsWith("#") && l.includes("="))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1).trim()]),
);
const USER = tokens["demo-user"];
const REVIEWER = tokens["demo-reviewer"];

async function call(path, token, init = {}) {
  const r = await fetch(API + path, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (!r.ok) throw new Error(`${init.method ?? "GET"} ${path}: ${r.status} ${await r.text()}`);
  return r.status === 204 ? null : r.json();
}
const me = (t) => call("/me", t);
const create = (request, scenario) =>
  call("/tasks", USER, { method: "POST", body: JSON.stringify({ request, scenario }) });
const status = async (id) => (await call(`/tasks/${id}`, USER)).status;
async function waitFor(id, want, timeout = 60_000) {
  const t0 = Date.now();
  for (;;) {
    const s = await status(id);
    if (want.includes(s)) return s;
    if (Date.now() - t0 > timeout) throw new Error(`task ${id} stuck in ${s}`);
    await new Promise((r) => setTimeout(r, 400));
  }
}
async function approveAll(id) {
  for (const a of await call(`/approvals?task_id=${id}`, REVIEWER)) {
    if (a.status !== "pending") continue;
    await call(`/approvals/${a.id}/decision`, REVIEWER, {
      method: "POST",
      body: JSON.stringify({ version: a.version, decision: { action: "approve" } }),
    });
  }
}

const identities = [
  { ...(await me(USER)), token: USER },
  { ...(await me(REVIEWER)), token: REVIEWER },
];
const sessionFor = (activeIdx) => JSON.stringify({ identities, activeId: identities[activeIdx].id });

// History content: a few finished runs in different states (fake mode: no cost).
const mode = process.env.CAPTURE_SEED === "0" ? "skip" : "seed";
if (mode === "seed") {
  const seeds = [
    ["Draft release notes for v0.4 from the changelog", "default"],
    ["Summarize three onboarding docs into one checklist", "partial_ok"],
    ["Write the incident summary for last night's outage", "revisions_exhausted"],
    ["Plan a migration with circular steps", "bad_plan_twice"],
  ];
  for (const [req, sc] of seeds) await create(req, sc);
}

const browser = await chromium.launch();
async function shoot(theme, width, name, fn) {
  const height = width < 600 ? 844 : 900;
  const ctx = await browser.newContext({
    viewport: { width, height },
    colorScheme: theme,
    deviceScaleFactor: width < 600 ? 2 : 1,
  });
  const page = await ctx.newPage();
  await page.addInitScript(([s, signedIn]) => {
    if (signedIn) sessionStorage.setItem("agentops.session", s);
  }, [sessionFor(0), name !== "sign-in"]);
  await fn(page);
  await page.waitForTimeout(350);
  const file = `${out}/${name}-${theme}-${width}.png`;
  await page.screenshot({ path: file, fullPage: true });
  console.log("saved", file);
  await ctx.close();
}

// The walkthrough task: research -> brief (revised once) -> email approval.
const demo = await create(
  "Compare pgvector with dedicated vector databases for our scale, write a one-page brief and email it to the team.",
  "ui_demo",
);

for (const theme of ["light", "dark"]) {
  await shoot(theme, 1440, "sign-in", async (p) => {
    await p.goto(`${UI}/sign-in`);
    await p.getByLabel("API token").waitFor();
  });
  await shoot(theme, 1440, "new-task", async (p) => {
    await p.goto(`${UI}/tasks/new`);
    await p.getByLabel("Request").fill(
      "Compare pgvector with dedicated vector databases for our scale, write a one-page brief and email it to the team.",
    );
    await p.getByLabel("Demo scenario (fake mode)").selectOption("ui_demo");
  });
}

// mid-run (light only: one live moment), then paused for approval, then completed
await shoot("light", 1440, "task-running", async (p) => {
  await p.goto(`${UI}/tasks/${demo.id}`);
  await p.getByText(/Working|Revising|In review/).first().waitFor({ timeout: 30_000 });
});
await waitFor(demo.id, ["awaiting_approval"]);
for (const theme of ["light", "dark"]) {
  await shoot(theme, 1440, "task-awaiting-approval", async (p) => {
    await p.goto(`${UI}/tasks/${demo.id}`);
    await p.getByText("Waiting for a reviewer").waitFor();
  });
}
await approveAll(demo.id);
await waitFor(demo.id, ["completed", "completed_partial"]);
for (const theme of ["light", "dark"]) {
  await shoot(theme, 1440, "task-completed", async (p) => {
    await p.goto(`${UI}/tasks/${demo.id}`);
    await p.getByRole("heading", { name: /Vector search/ }).waitFor();
    await p.locator("details summary", { hasText: "brief" }).first().click();
  });
  await shoot(theme, 1440, "tasks", async (p) => {
    await p.goto(`${UI}/tasks`);
    await p.getByRole("table").waitFor();
  });
}
await shoot("light", 390, "task-completed", async (p) => {
  await p.goto(`${UI}/tasks/${demo.id}`);
  await p.getByRole("heading", { name: /Vector search/ }).waitFor();
});
await shoot("dark", 390, "tasks", async (p) => {
  await p.goto(`${UI}/tasks`);
  await p.getByRole("table").waitFor();
});

await browser.close();
console.log("demo task", demo.id);
