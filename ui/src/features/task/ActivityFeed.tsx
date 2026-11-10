import { useEffect, useMemo, useRef, useState } from "react";
import type { StreamState } from "../../api/events";
import type { TaskEvent } from "../../api/types";
import { Icon } from "../../components/Icon";
import { EmptyState } from "../../components/ui";
import { clockTime, feedLine, type FeedTone } from "../../lib/format";

const DOT: Record<FeedTone, string> = {
  info: "bg-info-fg",
  success: "bg-success-fg",
  warning: "bg-warning-fg",
  danger: "bg-danger-fg",
  neutral: "bg-line-strong",
  violet: "bg-violet-fg",
};

const STREAM: Record<StreamState, { label: string; cls: string }> = {
  connecting: { label: "Connecting", cls: "text-fg-subtle" },
  live: { label: "Live", cls: "text-success-fg" },
  reconnecting: { label: "Reconnecting", cls: "text-warning-fg" },
  closed: { label: "Finished", cls: "text-fg-subtle" },
  error: { label: "Disconnected", cls: "text-danger-fg" },
};

export function StreamIndicator({ state }: { state: StreamState }) {
  const s = STREAM[state];
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${s.cls}`} role="status" aria-live="polite">
      <span
        aria-hidden
        className={`size-1.5 rounded-full bg-current ${state === "live" ? "animate-pulse-dot" : ""}`}
      />
      {s.label}
    </span>
  );
}

export function ActivityFeed({ events, state }: { events: TaskEvent[]; state: StreamState }) {
  const [showCalls, setShowCalls] = useState(false);
  const lines = useMemo(
    () => events.map(feedLine).filter((l) => l !== null && (showCalls || !l.detail)),
    [events, showCalls],
  );
  const box = useRef<HTMLOListElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  return (
    <div>
      <div className="flex items-center justify-between gap-2 border-b border-line px-4 py-2">
        <StreamIndicator state={state} />
        <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-fg-muted">
          <input
            type="checkbox"
            checked={showCalls}
            onChange={(e) => setShowCalls(e.target.checked)}
            className="size-3.5 accent-[var(--accent)]"
          />
          Model calls
        </label>
      </div>
      {lines.length === 0 ? (
        state === "connecting" ? (
          <p className="px-4 py-6 text-center text-sm text-fg-subtle">Connecting to the event stream…</p>
        ) : (
          <EmptyState icon="list" title="No activity yet" />
        )
      ) : (
        <ol
          ref={box}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
          aria-label="Activity"
          tabIndex={0} // scrollable: keyboard users must be able to reach and scroll it
          className="max-h-[440px] space-y-0.5 overflow-y-auto px-2 py-2"
        >
          {lines.map((l) => (
            <li key={l!.id} className="flex items-start gap-2.5 rounded-md px-2 py-1 hover:bg-surface-2">
              <span aria-hidden className={`mt-[7px] size-1.5 shrink-0 rounded-full ${DOT[l!.tone]}`} />
              <span className="min-w-0 flex-1 text-[13px] leading-5 break-words text-fg">{l!.text}</span>
              <time dateTime={l!.at} className="shrink-0 pt-px font-mono text-[11px] text-fg-subtle tabular-nums">
                {clockTime(l!.at)}
              </time>
            </li>
          ))}
        </ol>
      )}
      {state === "reconnecting" && (
        <p className="flex items-center gap-1.5 border-t border-line px-4 py-2 text-xs text-warning-fg">
          <Icon name="refresh" size={12} /> Connection lost, reconnecting with a new stream ticket…
        </p>
      )}
    </div>
  );
}
