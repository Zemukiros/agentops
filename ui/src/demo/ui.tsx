// UI for the recorded demo (ADR-053). Loaded only in VITE_DEMO builds (via React.lazy).
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { session } from "../api/session";
import { Icon } from "../components/Icon";
import { Button, Callout } from "../components/ui";
import { DEMO_TASK_ID, demo, recording, useDemo } from "./store";

export const SOURCE_URL = "https://github.com/Zemukiros/agentops";
const TASK_PATH = `/tasks/${DEMO_TASK_ID}`;

function signInDemo(): void {
  const [user, reviewer] = recording.identities;
  // tokens are just the identity ids: demo requests never leave the browser
  session.add({ ...user!, token: user!.id });
  session.add({ ...reviewer!, token: reviewer!.id });
}

/** Thin bar above every page: what this is, where the code is, replay. */
export function DemoBanner() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const s = useDemo();

  useEffect(() => demo.start(), []);

  // Every revealed event can change any view: refresh what's on screen (throttled).
  useEffect(() => {
    let pending: ReturnType<typeof setTimeout> | undefined;
    const off = demo.subscribe(() => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = undefined;
        void qc.invalidateQueries();
      }, 600);
    });
    return () => {
      off();
      if (pending) clearTimeout(pending);
    };
  }, [qc]);

  const done = s.revealed >= demo.total;
  return (
    <div
      role="region"
      aria-label="About this demo"
      className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-accent-soft px-4 py-2 text-[13px] text-fg sm:px-6 lg:px-8"
    >
      <span className="font-medium">Recorded demo of a real run.</span>
      <span className="text-fg-muted">No live backend: the real UI replays one real-model task.</span>
      {s.waitingFor ? (
        <Link to={`/approvals?id=${s.waitingFor}`} className="font-medium text-accent-ink underline underline-offset-2">
          Waiting for your approval →
        </Link>
      ) : (
        pathname !== TASK_PATH && (
          <Link to={TASK_PATH} className="font-medium text-accent-ink underline underline-offset-2">
            {done ? "See the result →" : "Watch the run →"}
          </Link>
        )
      )}
      <span className="ml-auto flex items-center gap-3">
        <button
          type="button"
          onClick={() => {
            demo.restart();
            navigate(TASK_PATH);
          }}
          className="inline-flex items-center gap-1 font-medium text-accent-ink hover:underline"
        >
          <Icon name="refresh" size={14} /> {done ? "Replay from start" : "Restart"}
        </button>
        <a href={SOURCE_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-accent-ink hover:underline">
          Source on GitHub <Icon name="external" size={13} />
        </a>
      </span>
    </div>
  );
}

/** Replaces the token form on the sign-in page. */
export function DemoEntry() {
  const navigate = useNavigate();
  return (
    <div className="rounded-2xl border border-line bg-surface p-6 shadow-card sm:p-7">
      <h1 className="text-lg font-semibold tracking-tight">AgentOps demo</h1>
      <p className="mt-2 text-sm leading-relaxed text-fg-muted">
        Watch a real AgentOps run: a supervisor plans, agents research and write, a reviewer checks each draft, and the
        email waits for <strong className="font-semibold text-fg">your</strong> approval before it is sent.
      </p>
      <p className="mt-2 text-sm leading-relaxed text-fg-muted">
        This is a recording of one task run with Claude Sonnet and Haiku ($0.0892, 17 model calls). It plays back in
        your browser; nothing is sent anywhere.
      </p>
      <Button
        variant="primary"
        className="mt-5 w-full"
        icon="spark"
        onClick={() => {
          signInDemo();
          demo.restart();
          navigate(TASK_PATH);
        }}
      >
        Enter demo
      </Button>
      <p className="mt-3 text-center text-xs text-fg-subtle">
        <a href={SOURCE_URL} target="_blank" rel="noreferrer" className="hover:text-fg hover:underline">
          Source and evaluation on GitHub
        </a>
      </p>
    </div>
  );
}

/** Shown instead of a feature that needs the live backend. */
export function DemoUnavailable({ what }: { what: string }) {
  return (
    <Callout tone="info" icon="alert" title={`${what} needs the live backend`}>
      <p>
        The recorded demo replays one finished run in your browser. Run AgentOps locally with Docker Compose to try
        this; scripted models are the default, so it costs nothing.
      </p>
      <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        <Link to={TASK_PATH} className="font-medium underline underline-offset-2">
          Back to the recorded task
        </Link>
        <a href={`${SOURCE_URL}#quick-start`} target="_blank" rel="noreferrer" className="font-medium underline underline-offset-2">
          Quick start on GitHub
        </a>
      </p>
    </Callout>
  );
}
