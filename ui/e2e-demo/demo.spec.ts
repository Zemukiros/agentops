/**
 * The recorded demo end to end (ADR-053): enter, watch the run, make both recorded decisions,
 * see the completed task and its trace. Asserts that no request leaves the preview server.
 *
 *   npx playwright test -c playwright.demo.config.ts
 */
import { expect, test } from "@playwright/test";
import { expectAccessible } from "../e2e/support";

test("recorded demo: enter, approve twice, completed with trace and cost", async ({ page, baseURL }) => {
  const outside: string[] = [];
  page.on("request", (r) => {
    if (!r.url().startsWith(baseURL!)) outside.push(r.url());
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "AgentOps demo" })).toBeVisible();
  await expectAccessible(page, "demo entry");
  await page.getByRole("button", { name: "Enter demo" }).click();
  await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("region", { name: "About this demo" })).toContainText("Recorded demo of a real run");

  // the plan appears and the first human decision is reached
  await expect(page.getByText("research", { exact: true }).first()).toBeVisible();
  for (const button of ["Accept drafts", "Approve"]) {
    const waiting = page.getByRole("link", { name: /Waiting for your approval/ });
    await expect(waiting).toBeVisible({ timeout: 120_000 });
    await waiting.click();
    await expect(page.getByRole("heading", { name: "Approvals", level: 1 })).toBeVisible();
    if (button === "Accept drafts") await expectAccessible(page, "demo approvals");
    await page.locator("form button[type=submit]").last().click();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await page.getByRole("link", { name: /Watch the run|See the result/ }).click();
    await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
  }

  await expect(page.getByText("Completed", { exact: true }).first()).toBeVisible({ timeout: 120_000 });
  await expect(page.getByRole("region", { name: "Deliverable" })).toContainText("emailed");
  await expectAccessible(page, "demo completed task");

  await page.getByRole("tab", { name: "Trace & cost" }).click();
  await expect(page.getByText("$0.0892").first()).toBeVisible();

  await page.getByRole("tab", { name: "Replay" }).click();
  await expect(page.getByText(/needs the live backend/)).toBeVisible();

  expect(outside).toEqual([]);
});
