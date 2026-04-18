import { Link, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";

const tz = (timeZone: string) =>
  new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone,
  }).format(new Date());

const NavItem = ({ to, label, active }: { to: string; label: string; active: boolean }) => (
  <Link
    to={to}
    className={`text-xs tracking-wide transition-colors ${
      active ? "text-foreground" : "text-muted-foreground hover:text-foreground"
    }`}
  >
    {label}
  </Link>
);

export const TopBar = () => {
  const { pathname } = useLocation();
  const [, force] = useState(0);
  useEffect(() => {
    const i = setInterval(() => force((n) => n + 1), 30_000);
    return () => clearInterval(i);
  }, []);
  return (
    <header className="fixed top-0 inset-x-0 z-40 border-b border-border bg-background/80 backdrop-blur">
      <div className="grid grid-cols-2 md:grid-cols-4 items-center px-6 md:px-10 h-12 text-xs">
        <Link to="/" className="font-medium tracking-tight">
          CREDENCE<sup className="text-[8px] ml-0.5">®</sup>
        </Link>
        <div className="hidden md:block text-center text-muted-foreground text-mono">
          San Francisco {tz("America/Los_Angeles")}
        </div>
        <div className="hidden md:block text-center text-muted-foreground text-mono">
          Hsinchu {tz("Asia/Taipei")}
        </div>
        <nav className="flex items-center gap-6 justify-end">
          <NavItem to="/validate" label="Validate" active={pathname === "/validate"} />
          <NavItem to="/discover" label="Pipeline" active={pathname.startsWith("/discover") || pathname.startsWith("/prospect")} />
          <NavItem to="/settings" label="Weights" active={pathname === "/settings"} />
        </nav>
      </div>
    </header>
  );
};
