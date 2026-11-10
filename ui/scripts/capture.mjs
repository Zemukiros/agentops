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
async function shoot(theme, width, name, fn, who = 0) {
  const height = width < 600 ? 844 : 900;
  const ctx = await browser.newContext({
    viewport: { width, height },
    colorScheme: theme,
    deviceScaleFactor: width < 600 ? 2 : 1,
  });
  const page = await ctx.newPage();
  await page.addInitScript(([s, signedIn]) => {
    if (signedIn) sessionStorage.setItem("agentops.session", s);
  }, [sessionFor(who), name !== "sign-in"]);
  await fn(page);
  await page.waitForTimeout(350);
  const file = `${out}/${name}-${theme}-${width}.png`;
  // grow the viewport to the page instead of fullPage: the sticky sidebar then spans it
  const h = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width, height: Math.min(Math.max(h, height), 4800) });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(250);
  await page.screenshot({ path: file });
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

// a plan awaiting approval (for the plan editor shot)
const planTask = await create("Plan the Q1 data platform migration across four teams", "low_confidence_plan");

// mid-run (light only: one live moment), then paused for approval, then completed
await shoot("light", 1440, "task-running", async (p) => {
  await p.goto(`${UI}/tasks/${demo.id}`);
  await p.getByText(/Working|Revising|In review/).first().waitFor({ timeout: 30_000 });
});
await waitFor(demo.id, ["awaiting_approval"]);
await waitFor(planTask.id, ["awaiting_approval"]);
const pending = await call("/approvals?state=pending", REVIEWER);
const demoApproval = pending.find((a) => a.task_id === demo.id);
const planApproval = pending.find((a) => a.task_id === planTask.id);
for (const theme of ["light", "dark"]) {
  await shoot(theme, 1440, "task-awaiting-approval", async (p) => {
    await p.goto(`${UI}/tasks/${demo.id}`);
    await p.getByText("Waiting for a reviewer").waitFor();
  });
  await shoot(
    theme,
    1440,
    "approvals",
    async (p) => {
      await p.goto(`${UI}/approvals?id=${demoApproval.id}`);
      await p.getByText("team@example.com").first().waitFor();
      await p.getByRole("radio", { name: "Edit & approve" }).click({ force: true });
    },
    1,
  );
}
await shoot(
  "light",
  1440,
  "approvals-plan-editor",
  async (p) => {
    await p.goto(`${UI}/approvals?id=${planApproval.id}`);
    await p.getByRole("radio", { name: "Edit plan" }).click({ force: true });
    await p.getByRole("button", { name: "Add subtask" }).waitFor();
  },
  1,
);
await shoot(
  "light",
  390,
  "approvals",
  async (p) => {
    await p.goto(`${UI}/approvals?id=${demoApproval.id}`);
    await p.getByText("team@example.com").first().waitFor();
  },
  1,
);
await approveAll(demo.id);
await waitFor(demo.id, ["completed", "completed_partial"]);

// a replay branch with an edited deliverable, for the replay + compare shots
const cps = await call(`/tasks/${demo.id}/checkpoints`, USER);
const before = cps.checkpoints.find((c) => c.forkable && c.next[0] === "final_review");
const branch = await call(`/tasks/${demo.id}/branches`, USER, {
  method: "POST",
  body: JSON.stringify({
    checkpoint_id: before.checkpoint_id,
    edits: {
      final:
        "# Vector search: start with pgvector\n\n**Recommendation:** pgvector now; re-evaluate at 10M vectors.\n\n- One database and one backup\n- Transactional with our data\n\nAn email with this recommendation was proposed to the team for approval.",
    },
  }),
});
await waitFor(branch.id, ["completed", "completed_partial"]);

for (const theme of ["light", "dark"]) {
  await shoot(theme, 1440, "task-completed", async (p) => {
    await p.goto(`${UI}/tasks/${demo.id}`);
    await p.getByRole("heading", { name: /^Compare pgvector/ }).first().waitFor();
    await p.locator("details summary", { hasText: "brief" }).first().click();
  });
  await shoot(theme, 1440, "trace-cost", async (p) => {
    await p.goto(`${UI}/tasks/${demo.id}?tab=trace`);
    await p.getByRole("list", { name: "Trace timeline" }).waitFor();
    await p.getByRole("list", { name: "Trace timeline" }).locator("summary").filter({ hasText: "supervisor · plan" }).first().click();
  });
  await shoot(theme, 1440, "replay", async (p) => {
    await p.goto(`${UI}/tasks/${demo.id}?tab=replay`);
    await p.getByRole("button", { name: /Deliverable written, before final review/ }).click();
    await p.getByText(/Defaults to the original task/).waitFor();
  });
  await shoot(theme, 1440, "compare", async (p) => {
    await p.goto(`${UI}/compare/${demo.id}/${branch.id}`);
    await p.getByRole("heading", { name: "Compare branches" }).waitFor();
    await p.getByText(/lines$/).first().waitFor();
  });
  await shoot(theme, 1440, "memory", async (p) => {
    await p.goto(`${UI}/memory`);
    await p.getByRole("list", { name: "Memories" }).waitFor();
    await p.getByRole("list", { name: "Memories" }).getByRole("button").first().click();
    await p.getByRole("button", { name: "Delete memory" }).waitFor();
  });
  await shoot(theme, 1440, "tasks", async (p) => {
    await p.goto(`${UI}/tasks?replays=1`);
    await p.getByRole("table").waitFor();
  });
}
await shoot("light", 390, "task-completed", async (p) => {
  await p.goto(`${UI}/tasks/${demo.id}`);
  await p.getByRole("heading", { name: /^Compare pgvector/ }).first().waitFor();
});
await shoot("dark", 390, "tasks", async (p) => {
  await p.goto(`${UI}/tasks`);
  await p.getByRole("table").waitFor();
});

await browser.close();
console.log("demo task", demo.id, "branch", branch.id, "plan task", planTask.id);
