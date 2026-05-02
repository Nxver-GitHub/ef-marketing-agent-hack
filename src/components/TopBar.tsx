import { Link, useLocation } from "react-router-dom";
import { memo, useEffect, useState } from "react";
import { useAccount } from "@/contexts/AccountContext";
import { EdgeFilterPills } from "@/components/EdgeFilterPills";

const tz = (timeZone: string) =>
  new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone,
  }).format(new Date());

/**
 * Clock readout — owns its own re-render interval so the rest of the TopBar
 * doesn't tick along with it.
 */
const ClockReadout = memo(function ClockReadout({
  city,
  timeZone,
}: {
  city: string;
  timeZone: string;
}) {
  const [t, setT] = useState(() => tz(timeZone));
  useEffect(() => {
    const i = setInterval(() => setT(tz(timeZone)), 30_000);
    return () => clearInterval(i);
  }, [timeZone]);
  return (
    <div className="hidden md:block text-center text-muted-foreground text-mono">
      {city} {t}
    </div>
  );
});

const NavItem = memo(function NavItem({
  to,
  label,
  active,
}: {
  to: string;
  label: string;
  active: boolean;
}) {
  return (
    <Link
      to={to}
      className={`text-xs tracking-wide transition-colors ${
        active ? "text-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </Link>
  );
});

/**
 * Account chip — shows the active account's display name, plus a
 * sign-out affordance for live-mode users. Demo mode gets a static
 * "DEMO" label to make the demo origin obvious.
 */
const AccountChip = memo(function AccountChip() {
  const { account, user, loading, signOut } = useAccount();
  if (loading) return null;
  if (!account)
    return (
      <Link
        to="/login"
        className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground hover:text-foreground"
      >
        Sign in
      </Link>
    );
  // Demo mode — distinctive styling, no sign-out (there's nothing to sign out of)
  if (account.id === "00000000-0000-0000-0000-000000000fff")
    return (
      <span className="text-[10px] uppercase tracking-[0.16em] text-warning">
        Demo
      </span>
    );
  return (
    <div className="flex items-center gap-3">
      <span
        className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground"
        title={user?.email ?? account.displayName}
      >
        {account.displayName}
      </span>
      <button
        type="button"
        onClick={() => void signOut()}
        className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground hover:text-foreground"
        aria-label="Sign out"
      >
        ↩
      </button>
    </div>
  );
});

const TopBarInner = () => {
  const { pathname } = useLocation();
  // Show the edge-filter sub-bar only on graph-centric routes where toggling
  // edge kinds affects what's rendered.
  const showFilterPills =
    pathname.startsWith("/discover") || pathname.startsWith("/org");

  return (
    <header className="fixed top-0 inset-x-0 z-40 border-b border-border bg-background/80 backdrop-blur">
      <div className="grid grid-cols-2 md:grid-cols-5 items-center px-6 md:px-10 h-12 text-xs">
        <Link to="/" className="font-medium tracking-tight">
          CREDENCE<sup className="text-[8px] ml-0.5">®</sup>
        </Link>
        <ClockReadout city="San Francisco" timeZone="America/Los_Angeles" />
        <ClockReadout city="Hsinchu" timeZone="Asia/Taipei" />
        <nav className="flex items-center gap-6 justify-end">
          <NavItem
            to="/discover"
            label="Pipeline"
            active={pathname.startsWith("/discover") || pathname.startsWith("/prospect")}
          />
          <NavItem
            to="/validate"
            label="Validate"
            active={pathname.startsWith("/validate")}
          />
          <NavItem
            to="/settings"
            label="Weights"
            active={pathname.startsWith("/settings")}
          />
        </nav>
        <div className="flex justify-end">
          <AccountChip />
        </div>
      </div>
      {showFilterPills ? (
        <div className="border-t border-border/60 bg-background/60 px-6 md:px-10 py-2">
          <EdgeFilterPills />
        </div>
      ) : null}
    </header>
  );
};

export const TopBar = memo(TopBarInner);
