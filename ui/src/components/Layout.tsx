import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { session, useSession, type Identity } from "../api/session";
import { useTheme, type ThemeChoice } from "../lib/theme";
import { Icon, type IconName } from "./Icon";
import { Badge } from "./StatusBadge";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  reviewerOnly?: boolean;
  badge?: ReactNode;
}

export function Logo() {
  return (
    <Link to="/tasks" className="flex items-center gap-2 rounded-md text-fg">
      <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">
        <rect width="32" height="32" rx="8" fill="var(--accent)" />
        <circle cx="10" cy="11" r="3.2" fill="var(--accent-fg)" />
        <circle cx="22" cy="11" r="3.2" fill="var(--accent-fg)" />
        <circle cx="16" cy="22" r="3.2" fill="var(--accent-fg)" />
        <path d="M10 11 16 22M22 11 16 22" stroke="var(--accent-fg)" strokeWidth="1.8" />
      </svg>
      <span className="text-[15px] font-semibold tracking-tight">AgentOps</span>
    </Link>
  );
}

function RoleBadge({ role }: { role: string }) {
  return role === "reviewer" ? (
    <Badge tone="violet" icon="shield">
      Reviewer
    </Badge>
  ) : (
    <Badge tone="neutral" icon="user">
      User
    </Badge>
  );
}

function initials(name: string): string {
  return name.slice(0, 2).toUpperCase();
}

export function IdentityMenu() {
  const { identities, active } = useSession();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  if (!active) return null;
  const others = identities.filter((i) => i.id !== active.id);
  const switchTo = (i: Identity) => {
    session.switchTo(i.id);
    setOpen(false);
  };

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2.5 rounded-lg border border-line bg-surface px-2.5 py-2 text-left hover:bg-surface-2"
      >
        <span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-full bg-accent-soft text-xs font-semibold text-accent-ink">
          {initials(active.name)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-fg">{active.name}</span>
          <span className="block text-xs text-fg-subtle">{active.role === "reviewer" ? "Reviewer" : "User"}</span>
        </span>
        <Icon name="chevronDown" className="text-fg-subtle" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Switch identity"
          className="absolute bottom-full left-0 z-30 mb-2 w-full min-w-60 rounded-xl border border-line bg-surface p-1.5 shadow-pop"
        >
          <p className="px-2 pt-1 pb-1.5 text-xs font-medium text-fg-subtle">Signed in as</p>
          <div className="flex items-center justify-between gap-2 px-2 pb-2">
            <span className="truncate text-sm font-medium">{active.name}</span>
            <RoleBadge role={active.role} />
          </div>
          {others.length > 0 && (
            <>
              <div className="my-1 border-t border-line" />
              <p className="px-2 pt-1 pb-1 text-xs font-medium text-fg-subtle">Switch to</p>
              {others.map((i) => (
                <button
                  key={i.id}
                  role="menuitem"
                  type="button"
                  onClick={() => switchTo(i)}
                  className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-surface-2"
                >
                  <span className="truncate">{i.name}</span>
                  <RoleBadge role={i.role} />
                </button>
              ))}
            </>
          )}
          <div className="my-1 border-t border-line" />
          <button
            role="menuitem"
            type="button"
            onClick={() => {
              setOpen(false);
              navigate("/sign-in");
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-surface-2"
          >
            <Icon name="plus" /> Add another token
          </button>
          <button
            role="menuitem"
            type="button"
            onClick={() => {
              session.clear();
              navigate("/sign-in");
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-danger-fg hover:bg-danger-bg"
          >
            <Icon name="logout" /> Sign out of all
          </button>
        </div>
      )}
    </div>
  );
}

export function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  const opts: { v: ThemeChoice; icon: IconName; label: string }[] = [
    { v: "light", icon: "sun", label: "Light" },
    { v: "system", icon: "monitor", label: "System" },
    { v: "dark", icon: "moon", label: "Dark" },
  ];
  return (
    <div role="radiogroup" aria-label="Color theme" className="flex rounded-lg border border-line bg-surface-2 p-0.5">
      {opts.map((o) => (
        <button
          key={o.v}
          type="button"
          role="radio"
          aria-checked={theme === o.v}
          title={o.label}
          onClick={() => setTheme(o.v)}
          className={`grid h-7 flex-1 place-items-center rounded-md text-fg-muted ${
            theme === o.v ? "bg-surface text-fg shadow-card" : "hover:text-fg"
          }`}
        >
          <Icon name={o.icon} size={15} label={o.label} />
        </button>
      ))}
    </div>
  );
}

function Nav({ items, onNavigate }: { items: NavItem[]; onNavigate?: () => void }) {
  const { active } = useSession();
  return (
    <nav aria-label="Main" className="space-y-0.5">
      {items
        .filter((i) => !i.reviewerOnly || active?.role === "reviewer")
        .map((i) => (
          <NavLink
            key={i.to}
            to={i.to}
            onClick={onNavigate}
            className={({ isActive }) =>
              `flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium ${
                isActive ? "bg-accent-soft text-accent-ink" : "text-fg-muted hover:bg-surface-2 hover:text-fg"
              }`
            }
          >
            <Icon name={i.icon} size={17} />
            <span className="flex-1">{i.label}</span>
            {i.badge}
          </NavLink>
        ))}
    </nav>
  );
}

function SidebarBody({ items, onNavigate }: { items: NavItem[]; onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-5 p-4">
      <div className="px-1 pt-1">
        <Logo />
      </div>
      <Link
        to="/tasks/new"
        onClick={onNavigate}
        className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg bg-accent px-3 text-sm font-medium text-accent-fg shadow-card hover:bg-accent-hover"
      >
        <Icon name="plus" /> New task
      </Link>
      <Nav items={items} onNavigate={onNavigate} />
      <div className="mt-auto space-y-2.5">
        <ThemeToggle />
        <IdentityMenu />
      </div>
    </div>
  );
}

export function Layout({ items }: { items: NavItem[] }) {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  // close the drawer on navigation; this is syncing UI state to the router
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => setOpen(false), [location.pathname]);

  return (
    <div className="min-h-dvh">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2 focus:shadow-pop"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-line bg-surface/95 px-4 backdrop-blur lg:hidden">
        <Logo />
        <button
          type="button"
          aria-label="Open menu"
          aria-expanded={open}
          aria-controls="mobile-nav"
          onClick={() => setOpen(true)}
          className="grid size-9 place-items-center rounded-lg text-fg-muted hover:bg-surface-2"
        >
          <Icon name="menu" size={20} />
        </button>
      </header>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Menu" id="mobile-nav">
          <button type="button" aria-label="Close menu" className="absolute inset-0 bg-black/40" onClick={() => setOpen(false)} />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] border-r border-line bg-surface shadow-pop">
            <SidebarBody items={items} onNavigate={() => setOpen(false)} />
          </div>
        </div>
      )}

      <div className="lg:flex lg:min-h-dvh">
        <aside className="hidden w-60 shrink-0 border-r border-line bg-surface lg:block">
          <div className="sticky top-0 h-dvh">
            <SidebarBody items={items} />
          </div>
        </aside>
        <main id="main" className="min-w-0 flex-1">
          <div className="mx-auto w-full max-w-[1440px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  meta,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-fg">{title}</h1>
        {description && <p className="mt-1 text-sm text-fg-muted">{description}</p>}
        {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
