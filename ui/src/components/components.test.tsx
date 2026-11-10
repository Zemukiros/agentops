import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { session } from "../api/session";
import { NewTask, parseBudget } from "../pages/NewTask";
import { SignIn } from "../pages/SignIn";
import { Markdown } from "./Markdown";
import { StatusBadge, TASK_STATUS } from "./StatusBadge";
import { EmptyState } from "./ui";

afterEach(() => {
  vi.unstubAllGlobals();
  session.clear();
});

describe("StatusBadge", () => {
  it.each(Object.entries(TASK_STATUS))("%s shows text, not only color", (status, spec) => {
    const { container } = render(<StatusBadge status={status} />);
    expect(screen.getByText(spec.label)).toBeInTheDocument();
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
  it("falls back to a readable label for unknown statuses", () => {
    render(<StatusBadge status="some_new_state" />);
    expect(screen.getByText("some new state")).toBeInTheDocument();
  });
});

describe("Markdown", () => {
  it("renders model output without raw HTML or script links", () => {
    const { container } = render(
      <Markdown>{'# Title\n\n<script>alert(1)</script><img src=x onerror="alert(1)">\n\n[x](javascript:alert(1)) [ok](https://example.com)'}</Markdown>,
    );
    expect(screen.getByRole("heading", { name: "Title" })).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    const links = [...container.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).not.toContain("javascript:alert(1)");
    const ok = screen.getByRole("link", { name: "ok" });
    expect(ok).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});

describe("EmptyState", () => {
  it("renders title, text and an action", () => {
    render(<EmptyState title="No tasks yet" action={<button>Start</button>}>Describe an outcome.</EmptyState>);
    expect(screen.getByText("No tasks yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
  });
});

function withProviders(ui: React.ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/" element={ui} />
          <Route path="/tasks" element={<p>tasks page</p>} />
          <Route path="/tasks/:id" element={<p>detail page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SignIn", () => {
  it("rejects an invalid token with a message tied to the field", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "invalid token" }), { status: 401 })));
    withProviders(<SignIn />);
    await userEvent.type(screen.getByLabelText("API token"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const msg = await screen.findByText("That token isn’t valid.");
    expect(screen.getByLabelText("API token")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("API token")).toHaveAttribute("aria-describedby", msg.id);
    expect(session.active()).toBeNull();
  });

  it("validates via /me, stores the identity and continues", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ id: "r1", name: "demo-reviewer", role: "reviewer" })));
    vi.stubGlobal("fetch", fetchMock);
    withProviders(<SignIn />);
    await userEvent.type(screen.getByLabelText("API token"), "  tok-r  ");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("tasks page")).toBeInTheDocument();
    expect(session.active()).toMatchObject({ name: "demo-reviewer", role: "reviewer", token: "tok-r" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/me$/);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-r");
  });
});

describe("NewTask", () => {
  it("validates budget fields", () => {
    expect(parseBudget({ max_usd: "", max_llm_calls: "", max_tokens: "" })).toEqual({ value: null, errors: {} });
    expect(parseBudget({ max_usd: "0.5", max_llm_calls: "20", max_tokens: "" }).value).toEqual({ max_usd: 0.5, max_llm_calls: 20 });
    expect(parseBudget({ max_usd: "-1", max_llm_calls: "2.5", max_tokens: "x" }).errors).toEqual({
      max_usd: "Must be a positive number",
      max_llm_calls: "Must be a whole number",
      max_tokens: "Must be a positive number",
    });
  });

  it("requires a request, then submits and opens the task", async () => {
    session.add({ id: "u", name: "demo-user", role: "user", token: "t" });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/tasks/scenarios"))
        return new Response(
          JSON.stringify({
            fake_mode: true,
            default: "ui_demo",
            scenarios: [
              { name: "ui_demo", description: "Full walkthrough" },
              { name: "default", description: "One writing subtask" },
            ],
          }),
        );
      expect(JSON.parse(String(init?.body))).toEqual({ request: "Write a brief", scenario: "ui_demo" });
      return new Response(JSON.stringify({ id: "t-1" }), { status: 201 });
    });
    vi.stubGlobal("fetch", fetchMock);
    withProviders(<NewTask />);
    await userEvent.click(screen.getByRole("button", { name: "Start task" }));
    expect(await screen.findByText("Describe what you want done.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Request"), "Write a brief");
    // ui_demo is preselected (no choice needed) and its description is shown
    const select = await screen.findByLabelText("Demo scenario (fake mode)");
    expect(select).toHaveValue("ui_demo");
    expect(screen.getByText("Full walkthrough", { selector: "p" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start task" }));
    await waitFor(() => expect(screen.getByText("detail page")).toBeInTheDocument());
  });
});
