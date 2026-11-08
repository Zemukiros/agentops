import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { ApiError, api } from "../api/client";
import { session, useSession } from "../api/session";
import type { Me } from "../api/types";
import { Icon } from "../components/Icon";
import { Logo, ThemeToggle } from "../components/Layout";
import { Badge } from "../components/StatusBadge";
import { Button, Field, inputClass } from "../components/ui";

export function SignIn() {
  const { identities, active } = useSession();
  const [token, setToken] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const t = token.trim();
    if (!t) {
      setError("Paste an API token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const me = await api<Me>("/me", { token: t });
      session.add({ ...me, token: t });
      setToken("");
      navigate("/tasks");
    } catch (err) {
      setError(
        err instanceof ApiError && err.isUnauthorized
          ? "That token isn’t valid."
          : `Couldn’t reach the API: ${err instanceof Error ? err.message : "network error"}`,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh flex-col">
      <div className="flex items-center justify-between px-5 py-4">
        <Logo />
        <div className="w-36">
          <ThemeToggle />
        </div>
      </div>
      <main id="main" className="grid flex-1 place-items-center px-4 pb-16">
        <div className="w-full max-w-md space-y-4">
          <div className="rounded-2xl border border-line bg-surface p-6 shadow-card sm:p-7">
            <h1 className="text-lg font-semibold tracking-tight">Sign in</h1>
            <p className="mt-1 text-sm text-fg-muted">
              Paste an API token. It stays in this browser tab only and is cleared when you close it.
            </p>
            <form onSubmit={submit} className="mt-5 space-y-4" noValidate>
              <Field
                label="API token"
                htmlFor="token"
                error={error}
                hint={
                  <>
                    Local dev tokens are in <code className="font-mono">.agentops-tokens</code> (created by{" "}
                    <code className="font-mono">make migrate</code>).
                  </>
                }
              >
                <div className="relative">
                  <input
                    id="token"
                    name="token"
                    type={show ? "text" : "password"}
                    autoComplete="off"
                    spellCheck={false}
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    aria-invalid={error ? true : undefined}
                    aria-describedby={error ? "token-error" : "token-hint"}
                    className={`${inputClass} pr-16 font-mono`}
                    placeholder="paste token"
                  />
                  <button
                    type="button"
                    onClick={() => setShow((s) => !s)}
                    className="absolute inset-y-1 right-1 rounded-md px-2 text-xs font-medium text-fg-muted hover:bg-surface-2"
                    aria-pressed={show}
                  >
                    {show ? "Hide" : "Show"}
                  </button>
                </div>
              </Field>
              <Button type="submit" variant="primary" className="w-full" busy={busy}>
                Sign in
              </Button>
            </form>
          </div>

          {identities.length > 0 && (
            <div className="rounded-2xl border border-line bg-surface p-4 shadow-card">
              <h2 className="px-1 pb-2 text-sm font-semibold">Signed in this tab</h2>
              <ul className="space-y-1">
                {identities.map((i) => (
                  <li key={i.id} className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        session.switchTo(i.id);
                        navigate("/tasks");
                      }}
                      className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-surface-2"
                    >
                      <span className="truncate text-sm font-medium">{i.name}</span>
                      {i.role === "reviewer" ? (
                        <Badge tone="violet" icon="shield">
                          Reviewer
                        </Badge>
                      ) : (
                        <Badge tone="neutral" icon="user">
                          User
                        </Badge>
                      )}
                      {active?.id === i.id && <span className="ml-auto text-xs text-fg-subtle">active</span>}
                    </button>
                    <button
                      type="button"
                      onClick={() => session.remove(i.id)}
                      className="grid size-8 place-items-center rounded-md text-fg-subtle hover:bg-surface-2 hover:text-danger-fg"
                      aria-label={`Forget ${i.name}`}
                    >
                      <Icon name="x" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
