// Primitives: buttons, cards, fields, empty/loading/error states.
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

type Variant = "primary" | "secondary" | "ghost" | "danger";
const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover border-transparent shadow-card",
  secondary: "bg-surface text-fg border-line-strong hover:bg-surface-2",
  ghost: "bg-transparent text-fg-muted border-transparent hover:bg-surface-2 hover:text-fg",
  danger: "bg-surface text-danger-fg border-danger-line hover:bg-danger-bg",
};

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: Variant;
    size?: "sm" | "md";
    icon?: IconName;
    busy?: boolean;
  }
>(function Button(
  { variant = "secondary", size = "md", icon, busy, children, className = "", disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-55 ${
        size === "sm" ? "h-8 px-2.5 text-xs" : "h-9 px-3.5 text-sm"
      } ${VARIANT[variant]} ${className}`}
      {...rest}
    >
      {busy ? <Spinner size={14} /> : icon ? <Icon name={icon} size={size === "sm" ? 14 : 16} /> : null}
      {children}
    </button>
  );
});

export function Card({
  title,
  actions,
  children,
  className = "",
  bodyClassName = "p-4",
  id,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
}) {
  return (
    <section
      aria-labelledby={title && id ? `${id}-title` : undefined}
      className={`rounded-xl border border-line bg-surface shadow-card ${className}`}
    >
      {(title || actions) && (
        <header className="flex min-h-12 items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          {title && (
            <h2 id={id ? `${id}-title` : undefined} className="text-sm font-semibold text-fg">
              {title}
            </h2>
          )}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export function Spinner({ size = 16, label }: { size?: number; label?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className="animate-spin"
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden className={`animate-pulse rounded-md bg-surface-3 ${className}`} />;
}

export function LoadingBlock({ label = "Loading", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2.5">
      <span className="sr-only">{label}…</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={`h-4 ${i % 3 === 2 ? "w-2/3" : "w-full"}`} />
      ))}
    </div>
  );
}

export function EmptyState({
  icon = "inbox",
  title,
  children,
  action,
}: {
  icon?: IconName;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <div className="mb-1 grid size-10 place-items-center rounded-full bg-surface-2 text-fg-subtle">
        <Icon name={icon} size={20} />
      </div>
      <p className="text-sm font-semibold text-fg">{title}</p>
      {children && <div className="max-w-sm text-sm text-fg-muted">{children}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong";
  return (
    <div role="alert" className="flex items-start gap-3 rounded-lg border border-danger-line bg-danger-bg p-3 text-sm text-danger-fg">
      <Icon name="alert" size={18} className="mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">Couldn’t load this</p>
        <p className="break-words opacity-90">{message}</p>
      </div>
      {onRetry && (
        <Button size="sm" variant="secondary" onClick={onRetry} icon="refresh">
          Retry
        </Button>
      )}
    </div>
  );
}

export function Callout({
  tone = "info",
  icon = "alert",
  title,
  children,
}: {
  tone?: "info" | "warning" | "danger" | "success" | "violet";
  icon?: IconName;
  title: string;
  children?: ReactNode;
}) {
  const cls = {
    info: "border-info-line bg-info-bg text-info-fg",
    warning: "border-warning-line bg-warning-bg text-warning-fg",
    danger: "border-danger-line bg-danger-bg text-danger-fg",
    success: "border-success-line bg-success-bg text-success-fg",
    violet: "border-violet-line bg-violet-bg text-violet-fg",
  }[tone];
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border p-3 text-sm ${cls}`}>
      <Icon name={icon} size={18} className="mt-0.5 shrink-0" />
      <div className="min-w-0">
        <p className="font-semibold">{title}</p>
        {children && <div className="mt-0.5 break-words opacity-95">{children}</div>}
      </div>
    </div>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line-strong bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-subtle shadow-[inset_0_1px_1px_rgb(0_0_0/0.03)] focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 aria-[invalid=true]:border-danger-fg";

/** inputClass without the full width, for inline controls with their own width */
export const inlineInputClass = inputClass.replace("w-full ", "");

export function Field({
  label,
  htmlFor,
  hint,
  error,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-fg">
        {label}
      </label>
      {children}
      {error ? (
        <p id={`${htmlFor}-error`} className="text-xs font-medium text-danger-fg">
          {error}
        </p>
      ) : hint ? (
        <p id={`${htmlFor}-hint`} className="text-xs text-fg-subtle">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded border border-line-strong bg-surface-2 px-1 font-mono text-[11px] text-fg-muted">
      {children}
    </kbd>
  );
}
