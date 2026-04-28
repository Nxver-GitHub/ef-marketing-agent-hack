import { Link, useLocation } from "react-router-dom";
import { memo, useEffect, useState } from "react";

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

const TopBarInner = () => {
  const { pathname } = useLocation();

  return (
    <header className="fixed top-0 inset-x-0 z-40 border-b border-border bg-background/80 backdrop-blur">
      <div className="grid grid-cols-2 md:grid-cols-4 items-center px-6 md:px-10 h-12 text-xs">
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
      </div>
    </header>
  );
};

export const TopBar = memo(TopBarInner);
