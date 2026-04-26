import { Link, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import type { EdgeKind } from "@/lib/graph";

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

// Edge-kind filter pill spec. `dotClass` uses the Tailwind classes wired up in
// tailwind.config.ts → colors.edge.* (driven by --edge-* CSS vars).
// `colleague` is intentionally omitted — it's auto-on for v1.
const EDGE_PILLS: ReadonlyArray<{ kind: EdgeKind; label: string; dotClass: string }> = [
  { kind: "reports_to", label: "Reports", dotClass: "bg-edge-reports" },
  { kind: "works_at", label: "Employer", dotClass: "bg-edge-employer" },
  { kind: "located_in", label: "Location", dotClass: "bg-edge-location" },
  { kind: "evidence_cited", label: "Evidence", dotClass: "bg-edge-evidence" },
  { kind: "scope_signal", label: "Scope", dotClass: "bg-edge-scope" },
  { kind: "partnership", label: "Partnership", dotClass: "bg-edge-partnership" },
  { kind: "past_employer", label: "Past empl.", dotClass: "bg-edge-past-empl" },
  { kind: "education", label: "Education", dotClass: "bg-edge-education" },
  { kind: "vertical", label: "Vertical", dotClass: "bg-edge-vertical" },
];

export interface TopBarProps {
  edgeKindsActive?: Set<EdgeKind>;
  onToggleEdgeKind?: (kind: EdgeKind) => void;
}

export const TopBar = ({ edgeKindsActive, onToggleEdgeKind }: TopBarProps = {}) => {
  const { pathname } = useLocation();
  const [, force] = useState(0);
  useEffect(() => {
    const i = setInterval(() => force((n) => n + 1), 30_000);
    return () => clearInterval(i);
  }, []);

  const showEdgeFilters = pathname === "/discover" && edgeKindsActive !== undefined;

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
          {showEdgeFilters && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] tracking-wider text-muted-foreground uppercase">
                Edges
              </span>
              <div className="flex items-center gap-1.5 flex-wrap">
                {EDGE_PILLS.map((pill) => {
                  const active = edgeKindsActive.has(pill.kind);
                  return (
                    <button
                      key={pill.kind}
                      type="button"
                      onClick={() => onToggleEdgeKind?.(pill.kind)}
                      className={`flex items-center gap-1.5 rounded-full border border-border py-[5px] px-[10px] transition-colors ${
                        active
                          ? "bg-muted text-foreground font-medium"
                          : "bg-transparent text-muted-foreground"
                      }`}
                    >
                      <span
                        className={`block h-2 w-2 rounded-full ${pill.dotClass}`}
                        style={{ opacity: active ? 1 : 0.35 }}
                      />
                      <span className="text-[11px] leading-none">{pill.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          <NavItem to="/validate" label="Validate" active={pathname === "/validate"} />
          <NavItem
            to="/discover"
            label="Pipeline"
            active={pathname.startsWith("/discover") || pathname.startsWith("/prospect")}
          />
          <NavItem to="/settings" label="Weights" active={pathname === "/settings"} />
        </nav>
      </div>
    </header>
  );
};
