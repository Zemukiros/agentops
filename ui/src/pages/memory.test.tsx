import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { session } from "../api/session";
import { MemoryPage } from "./Memory";

beforeAll(() => {
  // jsdom lacks <dialog> modality
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
});
afterEach(() => {
  vi.unstubAllGlobals();
  session.clear();
});

const mem = { id: "m1", kind: "episode", text: 'Past task "x" ended completed', importance: 0.5, outcome: "completed", source_task_id: "t-123456789", created_at: "2026-11-09T10:00:00Z", similarity: null, score: null };

function setup(handler: (url: string, init?: RequestInit) => Response) {
  session.add({ id: "u", name: "demo-user", role: "user", token: "t" });
  const f = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal("fetch", f);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/memory"]}>
        <MemoryPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return f;
}

describe("Memory browser", () => {
  it("delete all requires typing the phrase, then calls DELETE /memories", async () => {
    const f = setup((url, init) => (init?.method === "DELETE" ? new Response(JSON.stringify({ deleted: 1 })) : new Response(JSON.stringify(url.includes("/memories?") ? [mem] : []))));
    await screen.findByText(mem.text);
    await userEvent.click(screen.getByRole("button", { name: "Delete all" }));
    const confirm = within(await screen.findByRole("dialog"));
    expect(confirm.getByRole("button", { name: "Delete all" })).toBeDisabled();
    await userEvent.type(confirm.getByLabelText(/Type/), "delete al");
    expect(confirm.getByRole("button", { name: "Delete all" })).toBeDisabled();
    await userEvent.type(confirm.getByLabelText(/Type/), "l");
    await userEvent.click(confirm.getByRole("button", { name: "Delete all" }));
    await waitFor(() => expect(f.mock.calls.some(([u, i]) => i?.method === "DELETE" && /\/memories$/.test(String(u)))).toBe(true));
  });

  it("inspects one memory with a source-task link and deletes it after confirming", async () => {
    const f = setup((url, init) => {
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      if (url.endsWith("/memories/m1")) return new Response(JSON.stringify(mem));
      return new Response(JSON.stringify([mem]));
    });
    await userEvent.click(await screen.findByText(mem.text));
    expect(await screen.findByRole("link", { name: "t-123456" })).toHaveAttribute("href", "/tasks/t-123456789");
    await userEvent.click(screen.getByRole("button", { name: "Delete memory" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(f.mock.calls.some(([u, i]) => i?.method === "DELETE" && String(u).endsWith("/memories/m1"))).toBe(true));
  });

  it("searches by meaning", async () => {
    const f = setup((url) => new Response(JSON.stringify(url.includes("/search") ? [{ ...mem, similarity: 0.87 }] : [])));
    await screen.findByText("No memories yet");
    await userEvent.type(screen.getByLabelText("Search memories"), "vector db");
    expect(await screen.findByText("similarity 0.87")).toBeInTheDocument();
    expect(f.mock.calls.some(([u]) => String(u).includes("/memories/search?q=vector+db"))).toBe(true);
  });
});
