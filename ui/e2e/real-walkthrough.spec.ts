/**
 * REAL-MODEL UI walkthrough (Phase 6 step 7). Costs money: skipped unless REAL_UI_WALKTHROUGH=1.
 * Needs the stack in real mode (api + worker with LLM_MODE=real and real role models). The task
 * is created through the UI with a $0.20 per-task budget, which the budget ledger enforces
 * before every model call, so the run can't spend more than that.
 *
 *   REAL_UI_WALKTHROUGH=1 npx playwright test e2e/real-walkthrough.spec.ts
 *
 * Screenshots go to ../docs/screenshots/real-*.png; a JSON summary is printed.
 */
import { expect, test, type Page } from "@playwright/test";
import { API, identityButton, mailCount, signIn, switchIdentity, tokens } from "./support";

const MAX_USD = "0.20";
const REQUEST =
  "Write a brief of at most 150 words on when a team should choose pgvector over a dedicated " +
  "vector database, and email it to team@example.com.";
const SHOTS = process.env.REAL_UI_SHOTS ?? "../docs/screenshots";

test.skip(!process.env.REAL_UI_WALKTHROUGH, "real models: set REAL_UI_WALKTHROUGH=1 (costs up to $0.20)");
test.setTimeout(15 * 60_000);
test.use({ actionTimeout: 30_000 }); // a wrong locator fails fast instead of waiting out the test

async function detail(page: Page, id: string, token: string) {
  const r = await page.request.get(`${API}/tasks/${id}/detail`, { headers: { Authorization: `Bearer ${token}` } });
  return (await r.json()) as {
    status: string;
    approvals: { id: string; kind: string; status: string }[];
    actions: { tool: string; side_effect_status: string | null; outcome: { idempotency_key?: string | null } | null }[];
  };
}

test("real models through the UI: plan, live DAG, approval, deliverable, trace and cost", async ({ page }) => {
  const { user, reviewer } = tokens();
  await signIn(page, reviewer);
  await identityButton(page, /demo-reviewer/).click();
  await page.getByRole("menuitem", { name: /Add another token/ }).click();
  await signIn(page, user);

  // REAL_UI_TASK_ID continues an existing task (e.g. after fixing this script) instead of
  // paying for a new one.
  let id = process.env.REAL_UI_TASK_ID ?? "";
  if (!id) {
    await page.getByRole("link", { name: "New task" }).first().click();
    await expect(page.getByRole("heading", { name: "New task", level: 1 })).toBeVisible();
    await expect(page.getByLabel("Demo scenario (fake mode)")).toHaveCount(0); // real mode: no scenarios
    await page.getByLabel("Request", { exact: true }).fill(REQUEST);
    await page.getByText("Budget").first().click();
    await page.getByLabel("Max cost (USD)").fill(MAX_USD);
    await page.getByRole("button", { name: "Start task" }).click();
    await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
    id = page.url().split("/").pop()!;
    // live DAG while it runs
    await expect(page.getByText(/Running|Planning/).first()).toBeVisible({ timeout: 120_000 });
    await page.waitForTimeout(8000);
    await page.screenshot({ path: `${SHOTS}/real-running-1440.png`, fullPage: true });
  }

  const decided: string[] = [];
  const deadline = Date.now() + 12 * 60_000;
  let status = "";
  while (Date.now() < deadline) {
    const d = await detail(page, id, user);
    status = d.status;
    if (["completed", "completed_partial", "failed", "rejected", "budget_exceeded", "cancelled"].includes(status)) break;
    const pending = d.approvals.find((a) => a.status === "pending");
    if (status === "awaiting_approval" && pending && !decided.includes(pending.id)) {
      await switchIdentity(page, /demo-reviewer/);
      await page.goto(`/approvals?id=${pending.id}`);
      await expect(page.getByRole("heading", { name: "Approvals", level: 1 })).toBeVisible();
      await page.waitForTimeout(1500);
      if (decided.length === 0) await page.screenshot({ path: `${SHOTS}/real-approval-1440.png`, fullPage: true });
      // The default action of every approval kind is "approve" (Approve / Accept drafts /
      // Approve plan / Accept); submit the decision form with it.
      const submit = page.locator("form button[type=submit]").last();
      console.log(`deciding ${pending.kind}: ${await submit.innerText()}`);
      await submit.click();
      await expect(page.getByRole("alert")).toHaveCount(0);
      decided.push(pending.id);
      await switchIdentity(page, /demo-user/);
      await page.goto(`/tasks/${id}`);
    }
    await page.waitForTimeout(3000);
  }

  await page.goto(`/tasks/${id}`);
  await expect(page.getByRole("region", { name: "Deliverable" })).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SHOTS}/real-completed-1440.png`, fullPage: true });
  await page.goto(`/tasks/${id}?tab=trace`);
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SHOTS}/real-trace-cost-1440.png`, fullPage: true });

  const cost = await (
    await page.request.get(`${API}/tasks/${id}/cost`, { headers: { Authorization: `Bearer ${user}` } })
  ).json();
  const d = await detail(page, id, user);
  const sent = d.actions.filter((a) => a.tool === "send_email" && a.side_effect_status === "done");
  const mails = await Promise.all(sent.map((a) => mailCount(`${a.outcome?.idempotency_key}@agentops`)));
  console.log(JSON.stringify({ task: id, status, approvals: decided.length, cost, emails_done: sent.length, mailpit: mails }));
  expect(["completed", "completed_partial"]).toContain(status);
  expect(mails.every((n) => n === 1)).toBe(true); // each approved email delivered exactly once
});
