import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { session } from "../../api/session";
import type { Approval } from "../../api/types";
import { Approvals, usePendingApprovals } from "../../pages/Approvals";
import { buildDecision, initialForm } from "./DecisionForm";

const EMAIL_SCHEMA = {
  type: "object",
  required: ["to", "subject", "body"],
  properties: {
    to: { type: "string", maxLength: 254, pattern: "^[^@\\s<>,;]{1,64}@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)*\\.[A-Za-z]{2,63}$" },
    subject: { type: "string", minLength: 1, maxLength: 200 },
    body: { type: "string", minLength: 1, maxLength: 20000 },
  },
};

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "a1",
  task_id: "t1",
  kind: "approve_action",
  key: "action:h",
  status: "pending",
  version: 1,
  payload: {
    tool: "send_email",
    risk: "external",
    agent: "writing",
    subtask_id: "brief",
    args: { to: "team@example.com", subject: "Hi", body: "Body" },
    input_schema: EMAIL_SCHEMA,
  },
  decision: null,
  source: "graph",
  blocking: true,
  expires_at: "2026-11-10T12:00:00Z",
  created_at: "2026-11-09T12:00:00Z",
  decided_at: null,
  decided_by: null,
  task_kind: "task",
  task_request: "Write a brief and email it",
  ...over,
});

afterEach(() => {
  vi.unstubAllGlobals();
  session.clear();
});

describe("buildDecision", () => {
  it("approve, and edit with re-validated args", () => {
    const a = approval();
    expect(buildDecision(a, initialForm(a)).decision).toEqual({ action: "approve" });
    const f = { ...initialForm(a), action: "edit" as const };
    expect(buildDecision(a, f).decision).toEqual({
      action: "edit",
      args: { to: "team@example.com", subject: "Hi", body: "Body" },
    });
    const bad = { ...f, args: { ...f.args, to: "a@b.co, x@evil.example" } };
    expect(buildDecision(a, bad)).toEqual({ errors: { "args.to": "Invalid format" } });
  });

  it("take over needs a deliverable (except for actions); budget needs a raise", () => {
    const fin = approval({ kind: "final_review", payload: { deliverable: "", review: null } });
    expect(buildDecision(fin, { ...initialForm(fin), action: "take_over" }).errors.deliverable).toBeTruthy();
    const act = approval();
    expect(buildDecision(act, { ...initialForm(act), action: "take_over", note: "sent by hand" }).decision).toEqual({
      action: "take_over",
      note: "sent by hand",
    });
    const b = approval({ kind: "budget", payload: { max_usd: 1 } });
    const bf = initialForm(b);
    expect(bf.budget.max_usd).toBe("2");
    expect(buildDecision(b, bf).decision).toEqual({ action: "approve", max_usd: 2 });
    expect(buildDecision(b, { ...bf, budget: { max_usd: "", max_llm_calls: "", max_tokens: "" } }).errors["budget.max_usd"]).toBe(
      "Raise at least one limit.",
    );
  });

  it("side-effect reviews only record what happened", () => {
    const s = approval({ kind: "side_effect_review", blocking: false, payload: { tool: "send_email", args: {} } });
    expect(buildDecision(s, initialForm(s)).decision).toEqual({ action: "confirmed_done" });
  });
});

function Badge() {
  const q = usePendingApprovals(true); // what the app shell's nav renders for reviewers
  return <span data-testid="badge">{q.data?.length ?? "…"}</span>;
}

function renderInbox({ withBadge = false } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/approvals"]}>
        {withBadge && <Badge />}
        <Approvals />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Approvals inbox", () => {
  it("keeps the reviewer's edits on a 409 and explains what happened", async () => {
    session.add({ id: "r", name: "demo-reviewer", role: "reviewer", token: "t" });
    let decided = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          decided = true;
          return new Response(JSON.stringify({ detail: "approval changed" }), { status: 409 });
        }
        if (url.includes("/approvals/a1")) return new Response(JSON.stringify(approval({ version: 2 })));
        return new Response(JSON.stringify([approval({ version: decided ? 2 : 1 })]));
      }),
    );
    renderInbox();
    await userEvent.click(await screen.findByRole("radio", { name: "Edit & approve" }));
    const subject = screen.getByLabelText("Subject");
    await userEvent.clear(subject);
    await userEvent.type(subject, "Edited subject");
    await userEvent.click(screen.getByRole("button", { name: "Edit & approve" }));
    expect(await screen.findByText("Changed by someone else")).toBeInTheDocument();
    expect(screen.getByLabelText("Subject")).toHaveValue("Edited subject"); // input kept
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("users see the item but can't decide it", async () => {
    session.add({ id: "u", name: "demo-user", role: "user", token: "t" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([approval()]))));
    renderInbox();
    expect(await screen.findByText("A reviewer decides this")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).toBeNull();
  });

  it("works next to the nav badge's pending query (regression: shared cache key crashed the page)", async () => {
    session.add({ id: "r", name: "demo-reviewer", role: "reviewer", token: "t" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([approval()]))));
    renderInbox({ withBadge: true });
    expect(await screen.findByRole("radio", { name: "Edit & approve" })).toBeInTheDocument();
    expect(await screen.findByTestId("badge")).toHaveTextContent("1");
  });

  it("shows an empty state", async () => {
    session.add({ id: "r", name: "demo-reviewer", role: "reviewer", token: "t" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
    renderInbox();
    expect(await screen.findByText("Nothing waiting")).toBeInTheDocument();
  });
});
