import { expect, test } from "@playwright/test";
import { API, MAILPIT, expectAccessible, identityButton, mailCount, signIn, switchIdentity, tokens } from "./support";

const created: string[] = [];

test.afterAll(async () => {
  // Leave no e2e runs behind: DELETE /tasks purges each task's tree (branches, memories).
  const { user } = tokens();
  for (const id of created) {
    await fetch(`${API}/tasks/${id}`, { method: "DELETE", headers: { Authorization: `Bearer ${user}` } });
  }
});

const REQUEST = `Compare pgvector with dedicated vector databases and email the team a brief (e2e ${Date.now()})`;

test("sign in -> live DAG -> approve email -> deliverable -> trace -> memory -> replay diff", async ({ page }) => {
  // E2E_CPU_THROTTLE=4 approximates a slow CI runner locally (timing races show up there).
  const throttle = Number(process.env.E2E_CPU_THROTTLE ?? 0);
  if (throttle > 1) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: throttle });
  }
  const { user, reviewer } = tokens();

  // 1. sign in as the user, then add the reviewer identity (switchable)
  await page.goto("/sign-in");
  await expectAccessible(page, "sign-in");
  await signIn(page, reviewer);
  await identityButton(page, /demo-reviewer/).click();
  await page.getByRole("menuitem", { name: /Add another token/ }).click();
  await signIn(page, user);
  await expect(identityButton(page, /demo-user/)).toBeVisible();
  await expectAccessible(page, "tasks");

  // 2. submit the walkthrough scenario
  await page.getByRole("link", { name: "New task" }).first().click();
  // Wait for the page itself: during the route change the history page's "Search requests"
  // box is still there, and a non-exact "Request" label would match it (CI flake, PR #4).
  await expect(page.getByRole("heading", { name: "New task", level: 1 })).toBeVisible();
  await page.getByLabel("Request", { exact: true }).fill(REQUEST);
  await expect(page.getByLabel("Request", { exact: true })).toHaveValue(REQUEST);
  await expect(page.getByLabel("Demo scenario (fake mode)")).toHaveValue("ui_demo"); // the default
  await expectAccessible(page, "new task");
  await page.getByRole("button", { name: "Start task" }).click();
  await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
  const taskId = page.url().split("/").pop()!;
  created.push(taskId);

  // 3. the DAG updates live (no reload): research works, is accepted, then brief starts
  const graph = page.getByRole("group", { name: /Plan graph/ });
  await expect(graph.getByLabel(/Subtask research, research/)).toBeVisible();
  await expect(graph.getByLabel(/Subtask research, research, accepted/)).toBeVisible();
  await expect(graph.getByLabel(/Subtask brief, writing, (working|in review|revising|accepted)/)).toBeVisible();
  await expect(page.getByText("Waiting for a reviewer")).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expectAccessible(page, "task detail (awaiting approval)");

  // 4. the reviewer approves the email in the inbox
  await switchIdentity(page, /demo-reviewer/);
  await page.getByRole("link", { name: /Approvals/ }).click();
  await expect(page.getByRole("heading", { name: "Approvals", level: 1 })).toBeVisible();
  const row = page.getByRole("button", { name: new RegExp(`Action.*task ${taskId.slice(0, 8)}`) });
  await row.click();
  await expect(page.getByText("team@example.com").first()).toBeVisible();
  await expectAccessible(page, "approvals");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(row).toHaveCount(0); // decided: gone from the pending list

  // 5. back as the owner: completed, deliverable shown, one email in Mailpit
  await switchIdentity(page, /demo-user/);
  await page.goto(`/tasks/${taskId}`);
  await expect(page.getByText("Completed").first()).toBeVisible();
  // fake-mode text is titled with the request's topic
  await expect(page.getByRole("heading", { name: /^Compare pgvector with dedicated vector databases/ })).toBeVisible();
  const mail = page.getByRole("link", { name: /View email in Mailpit/ });
  await expect(mail).toHaveAttribute("href", new RegExp(`^${MAILPIT}/view/`));
  const messageId = await page.evaluate(async (id) => {
    const r = await fetch(`http://localhost:8000/tasks/${id}/detail`, {
      headers: { Authorization: `Bearer ${JSON.parse(sessionStorage.getItem("agentops.session")!).identities.find((i: { name: string }) => i.name === "demo-user").token}` },
    });
    return ((await r.json()) as { actions: { outcome: { idempotency_key: string } }[] }).actions[0]!.outcome.idempotency_key;
  }, taskId);
  expect(await mailCount(`${messageId}@agentops`)).toBe(1);
  await expectAccessible(page, "task detail (completed)");

  // 6. trace shows cost, calls and the time split
  await page.getByRole("tab", { name: "Trace & cost" }).click();
  await expect(page.getByText("Model calls").first()).toBeVisible();
  await expect(page.getByRole("img", { name: /Compute .*Human wait .*Queue/ })).toBeVisible();
  await expect(page.getByRole("list", { name: "Trace timeline" }).getByText(/supervisor · plan/).first()).toBeVisible();
  await expectAccessible(page, "trace & cost");

  // 7. replay: fork before the final review with an edited deliverable, run, compare
  await page.getByRole("tab", { name: "Replay" }).click();
  await page.getByRole("button", { name: /Deliverable written, before final review/ }).click();
  await expect(page.getByText(/Defaults to the original task’s limits/)).toBeVisible();
  const edited = "# Vector search: start with pgvector\n\nEdited in a replay branch.";
  await page.getByLabel("Deliverable", { exact: true }).fill(edited);
  await expectAccessible(page, "replay");
  await page.getByRole("button", { name: "Fork and run" }).click();
  await expect(page.getByText(/Replay of/)).toBeVisible();
  await expect(page.getByText("Completed").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Vector search: start with pgvector" })).toBeVisible();
  // the branch reports the original's email outcome and sends nothing: still exactly one
  // message with that Message-ID (forks before execution: tests/integration/test_replay.py)
  await expect(page.getByText("Done", { exact: true }).first()).toBeVisible();
  expect(await mailCount(`${messageId}@agentops`)).toBe(1);
  await page.getByRole("tab", { name: "Replay" }).click();
  await page.getByLabel("Compare this task with").selectOption({ label: `original ${taskId.slice(0, 8)}` });
  await page.getByRole("link", { name: "Compare side by side" }).click();
  await expect(page.getByRole("heading", { name: "Compare branches" })).toBeVisible();
  await expect(page.getByText("Edited in a replay branch.").first()).toBeVisible();
  await expect(page.getByText(/\+\d+ −\d+ lines/)).toBeVisible();
  await expectAccessible(page, "compare");

  // 8. memory: the original task's episode is visible, inspectable and deletable
  await page.getByRole("link", { name: "Memory" }).click();
  await expect(page.getByRole("heading", { name: "Memory", level: 1 })).toBeVisible();
  await page.getByLabel("Search memories").fill(REQUEST);
  const hit = page.getByRole("list", { name: "Memories" }).getByRole("button", { name: new RegExp(REQUEST.slice(-12).replace(/[()]/g, ".")) }).first();
  await expect(hit).toBeVisible();
  await hit.click();
  await expect(page.getByRole("link", { name: taskId.slice(0, 8) })).toBeVisible();
  await expectAccessible(page, "memory");
  await page.getByRole("button", { name: "Delete memory" }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText("Select a memory")).toBeVisible();
});
