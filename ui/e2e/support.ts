import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export const API = process.env.API_URL ?? "http://localhost:8000";
export const MAILPIT = process.env.MAILPIT_URL ?? "http://localhost:8025";

export function tokens(): { user: string; reviewer: string } {
  const file = process.env.TOKENS_FILE ?? resolve(process.cwd(), "../.agentops-tokens") // run from ui/;
  const t = Object.fromEntries(
    readFileSync(file, "utf8")
      .split("\n")
      .filter((l) => l.includes("=") && !l.startsWith("#"))
      .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1).trim()]),
  );
  return { user: t["demo-user"]!, reviewer: t["demo-reviewer"]! };
}

/** axe: no serious or critical WCAG 2.x A/AA violations on the current page. */
export async function expectAccessible(page: Page, label: string): Promise<void> {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const bad = r.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(
    bad.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")})`),
    `accessibility on ${label}`,
  ).toEqual([]);
}

export async function signIn(page: Page, token: string): Promise<void> {
  await page.goto("/sign-in");
  await page.getByLabel("API token").fill(token);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/tasks$/);
}

export async function switchIdentity(page: Page, name: RegExp): Promise<void> {
  await page.getByRole("button", { name: /demo-(user|reviewer)/ }).last().click();
  await page.getByRole("menuitem", { name }).click();
}

export async function mailCount(messageId: string): Promise<number> {
  const r = await fetch(`${MAILPIT}/api/v1/search?query=${encodeURIComponent(`message-id:${messageId}`)}`);
  return ((await r.json()) as { messages_count: number }).messages_count;
}
