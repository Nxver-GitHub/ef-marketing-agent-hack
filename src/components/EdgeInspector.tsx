/**
 * EdgeInspector — right-rail inspector for a selected connection (edge).
 *
 * Pure presentational. The parent owns the selection and passes the edge
 * blob in; the inspector renders header (X ↔ Y), strength breakdown,
 * per-source-type evidence rows, and the "Use this connection" CTA.
 *
 * Visual language matches NodeInspector: same border/spacing/font sizes,
 * Eyebrow caps, small grid for stats, rounded card rows for evidence.
 *
 * Evidence templates mirror the warmPaths.ts explanation/opener generators
 * (CLAUDE.md L711-767) so the UI surfaces the same per-kind specifics that
 * power the outreach copy.
 */
import type { JSX } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

// ── Public types ────────────────────────────────────────────────────────────

export interface EdgeEvidence {
  source_type:
    | "patent"
    | "paper"
    | "career_overlap"
    | "standards"
    | "conference"
    | "cohort"
    | "unknown";
  source_id?: string | null;
  structured_value?: Record<string, unknown> | null;
  collected_at?: string | null;
  url?: string | null;
}

export interface EdgeInspectorPerson {
  id: string;
  canonical_name: string;
  current_title?: string | null;
  current_company_name?: string | null;
}

export interface EdgeInspectorEdge {
  id: string;
  /** e.g. "patent_co_inventor", "career_overlap_general". */
  connection_type: string;
  /** [0, 1] base strength from STRENGTH_TABLE. */
  base_strength: number;
  recency_factor?: number | null;
  frequency_factor?: number | null;
  corroboration_factor?: number | null;
  /** [0, 0.99] computed strength used for ranking. */
  computed_strength: number;
  evidence: EdgeEvidence[];
  source_person: EdgeInspectorPerson;
  target_person: EdgeInspectorPerson;
}

export interface EdgeInspectorProps {
  edge: EdgeInspectorEdge | null;
  onUseConnection?: (edge: EdgeInspectorEdge) => void;
  onDismiss?: () => void;
  className?: string;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function fmtFactor(n: number | null | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return n.toFixed(2);
}

function fmtComputed(n: number): string {
  if (!Number.isFinite(n)) return "0.00";
  // computed_strength is bounded 0..0.99 — show 2 decimals.
  return Math.max(0, Math.min(0.99, n)).toFixed(2);
}

function strengthBarColor(n: number): string {
  if (n >= 0.7) return "bg-emerald-500";
  if (n >= 0.4) return "bg-amber-500";
  return "bg-muted-foreground/40";
}

function readStr(
  obj: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  if (!obj) return null;
  const v = obj[key];
  if (typeof v === "string" && v.length > 0) return v;
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return null;
}

function readNum(
  obj: Record<string, unknown> | null | undefined,
  key: string,
): number | null {
  if (!obj) return null;
  const v = obj[key];
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.length > 0 && Number.isFinite(Number(v))) {
    return Number(v);
  }
  return null;
}

function readBool(
  obj: Record<string, unknown> | null | undefined,
  key: string,
): boolean | null {
  if (!obj) return null;
  const v = obj[key];
  return typeof v === "boolean" ? v : null;
}

// ── Sub-components ──────────────────────────────────────────────────────────

const Eyebrow = ({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) => (
  <div
    className={cn(
      "text-[10px] uppercase tracking-[0.18em] text-muted-foreground",
      className,
    )}
  >
    {children}
  </div>
);

interface EvidenceRowProps {
  evidence: EdgeEvidence;
}

function EvidenceRow({ evidence }: EvidenceRowProps): JSX.Element {
  const sv = evidence.structured_value ?? null;

  switch (evidence.source_type) {
    case "patent": {
      const num = readStr(sv, "patent_number") ?? readStr(sv, "patentNumber");
      const title = readStr(sv, "patent_title") ?? readStr(sv, "patentTitle");
      const assignee = readStr(sv, "assignee");
      const directYear = readStr(sv, "year");
      const dateStr = readStr(sv, "filing_date") ?? readStr(sv, "filingDate");
      const year =
        directYear ?? (dateStr && dateStr.length >= 4 ? dateStr.slice(0, 4) : null);
      return (
        <EvidenceCard>
          <div className="text-xs font-medium leading-snug">
            <span aria-hidden>📜</span> Patent {num ?? "(unknown)"}
            {title ? `: ${title}` : ""}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {[assignee, year].filter(Boolean).join(" · ") || "Details unavailable"}
          </div>
          {evidence.url && (
            <a
              href={evidence.url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-[11px] text-foreground hover:underline"
            >
              View on USPTO ↗
            </a>
          )}
        </EvidenceCard>
      );
    }

    case "paper": {
      const title = readStr(sv, "paper_title") ?? readStr(sv, "paperTitle");
      const venue = readStr(sv, "venue");
      const year =
        readStr(sv, "year") ?? readStr(sv, "publication_year");
      const cites =
        readNum(sv, "citation_count") ?? readNum(sv, "citationCount");
      const doi = readStr(sv, "doi") ?? readStr(sv, "DOI");
      return (
        <EvidenceCard>
          <div className="text-xs font-medium leading-snug">
            <span aria-hidden>📄</span> {title ?? "(untitled paper)"}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {[
              venue,
              year,
              typeof cites === "number" ? `${cites} citations` : null,
            ]
              .filter(Boolean)
              .join(" · ") || "Details unavailable"}
          </div>
          {doi && (
            <a
              href={`https://doi.org/${doi}`}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-[11px] text-foreground hover:underline"
            >
              DOI: {doi} ↗
            </a>
          )}
        </EvidenceCard>
      );
    }

    case "career_overlap": {
      const company =
        readStr(sv, "company") ??
        readStr(sv, "company_name") ??
        readStr(sv, "companyName");
      const startYear =
        readStr(sv, "start_year") ??
        readStr(sv, "startYear") ??
        readStr(sv, "overlap_start_year") ??
        readStr(sv, "overlapStartYear");
      const endYear =
        readStr(sv, "end_year") ??
        readStr(sv, "endYear") ??
        readStr(sv, "overlap_end_year") ??
        readStr(sv, "overlapEndYear");
      const sameTeam =
        readBool(sv, "same_team") ?? readBool(sv, "sameTeam");
      const team =
        readStr(sv, "team") ??
        readStr(sv, "team_name") ??
        readStr(sv, "inferred_team");
      return (
        <EvidenceCard>
          <div className="text-xs font-medium leading-snug">
            <span aria-hidden>💼</span> Both at {company ?? "the same company"}{" "}
            {startYear ? `from ${startYear} ` : ""}
            {endYear ? `to ${endYear}` : startYear ? "to present" : ""}
          </div>
          {(sameTeam || team) && (
            <div className="mt-1 text-[11px] text-muted-foreground">
              {sameTeam
                ? `Same team${team ? ` · ${team}` : ""}`
                : team
                  ? `Team: ${team}`
                  : ""}
            </div>
          )}
        </EvidenceCard>
      );
    }

    case "standards": {
      const org =
        readStr(sv, "organization") ?? readStr(sv, "org") ?? readStr(sv, "body");
      const committee = readStr(sv, "committee");
      const role = readStr(sv, "role");
      const years = readStr(sv, "years");
      return (
        <EvidenceCard>
          <div className="text-xs font-medium leading-snug">
            <span aria-hidden>📋</span> {org ?? "(unknown body)"}
            {committee ? ` / ${committee}` : ""}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {[role, years].filter(Boolean).join(" · ") || "Membership details unavailable"}
          </div>
        </EvidenceCard>
      );
    }

    case "conference": {
      const event = readStr(sv, "event") ?? readStr(sv, "event_name");
      const year = readStr(sv, "year");
      return (
        <EvidenceCard>
          <div className="text-xs font-medium leading-snug">
            <span aria-hidden>🎤</span> Co-presented at {event ?? "a conference"}
            {year ? ` (${year})` : ""}
          </div>
        </EvidenceCard>
      );
    }

    case "cohort": {
      const school =
        readStr(sv, "school") ??
        readStr(sv, "institution") ??
        readStr(sv, "school_name");
      const degree = readStr(sv, "degree") ?? readStr(sv, "program");
      const yearOverlap =
        readStr(sv, "year_overlap") ??
        readStr(sv, "yearOverlap") ??
        readStr(sv, "overlap_years");
      return (
        <EvidenceCard>
          <div className="text-xs font-medium leading-snug">
            <span aria-hidden>🎓</span> {school ?? "(unknown school)"}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {[degree, yearOverlap ? `overlap: ${yearOverlap}` : null]
              .filter(Boolean)
              .join(" · ") || "Cohort details unavailable"}
          </div>
        </EvidenceCard>
      );
    }

    case "unknown":
    default: {
      let snippet = "";
      try {
        snippet = sv ? JSON.stringify(sv).slice(0, 200) : "(no structured value)";
      } catch {
        snippet = "(unserializable structured value)";
      }
      return (
        <EvidenceCard>
          <div className="text-[11px] text-muted-foreground font-mono break-all leading-relaxed">
            {snippet}
          </div>
        </EvidenceCard>
      );
    }
  }
}

const EvidenceCard = ({ children }: { children: React.ReactNode }) => (
  <div className="rounded border p-2 bg-muted/40">{children}</div>
);

// ── Main ────────────────────────────────────────────────────────────────────

export function EdgeInspector(props: EdgeInspectorProps): JSX.Element {
  const { edge, onUseConnection, onDismiss, className } = props;

  if (!edge) {
    return (
      <aside
        className={cn(
          "w-[360px] shrink-0 border border-dashed border-border bg-muted/10 p-4 text-xs text-muted-foreground",
          className,
        )}
      >
        Click an edge to see its evidence.
      </aside>
    );
  }

  const computed = edge.computed_strength;
  const sourceName = edge.source_person.canonical_name;
  const targetName = edge.target_person.canonical_name;
  const evidenceCount = edge.evidence?.length ?? 0;

  return (
    <aside
      className={cn(
        "w-[360px] shrink-0 border border-border bg-background p-4 space-y-4",
        className,
      )}
    >
      {/* ── Section 1: Header ─────────────────────────────────────────────── */}
      <header className="space-y-1.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Eyebrow>Connection</Eyebrow>
            <div className="text-base font-medium leading-snug mt-1 truncate">
              <span>{sourceName}</span>
              <span className="mx-1.5 text-muted-foreground" aria-hidden>
                ↔
              </span>
              <span>{targetName}</span>
            </div>
            <div className="text-[11px] text-muted-foreground mt-0.5">
              {`via ${edge.connection_type.replace(/_/g, " ")}`}
            </div>
          </div>
          {onDismiss && (
            <button
              type="button"
              onClick={onDismiss}
              className="text-muted-foreground hover:text-foreground transition-colors p-1 -m-1 shrink-0"
              aria-label="Close edge inspector"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </header>

      {/* ── Section 2: Strength breakdown ─────────────────────────────────── */}
      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <Eyebrow>Computed strength</Eyebrow>
          <span className="text-mono text-[11px] text-muted-foreground">
            cap 0.99
          </span>
        </div>
        <div
          className="text-3xl leading-none font-mono"
          aria-label={`Computed strength ${fmtComputed(computed)}`}
        >
          {fmtComputed(computed)}
        </div>
        <div
          className="h-1.5 bg-muted overflow-hidden rounded-sm"
          role="progressbar"
          aria-valuenow={Math.round(computed * 100)}
          aria-valuemin={0}
          aria-valuemax={99}
          data-testid="edge-strength-bar"
        >
          <div
            data-testid="edge-strength-bar-fill"
            className={cn("h-full transition-all", strengthBarColor(computed))}
            style={{ width: `${Math.max(0, Math.min(99, computed * 100))}%` }}
          />
        </div>

        <div className="grid grid-cols-4 gap-px bg-border border border-border mt-2">
          <FactorCell label="Base" value={fmtFactor(edge.base_strength)} />
          <FactorCell label="Recency" value={fmtFactor(edge.recency_factor)} />
          <FactorCell label="Frequency" value={fmtFactor(edge.frequency_factor)} />
          <FactorCell
            label="Corrob."
            value={fmtFactor(edge.corroboration_factor)}
          />
        </div>
        <p className="text-[10px] text-muted-foreground leading-relaxed">
          computed_strength = base × recency × (1 + log(corroboration_count) ×
          0.15) × (1 + source_type_count × 0.10), capped 0.99
        </p>
      </section>

      {/* ── Section 3: Evidence list ──────────────────────────────────────── */}
      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <Eyebrow>Evidence ({evidenceCount} sources)</Eyebrow>
        </div>
        {evidenceCount > 0 ? (
          <ul className="space-y-2">
            {edge.evidence.map((ev, i) => (
              <li key={`${ev.source_type}|${ev.source_id ?? i}|${i}`}>
                <EvidenceRow evidence={ev} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-muted-foreground">
            No structured evidence attached to this connection.
          </p>
        )}
      </section>

      {/* ── Section 4: Action ─────────────────────────────────────────────── */}
      {onUseConnection && (
        <section className="space-y-1.5">
          <button
            type="button"
            onClick={() => onUseConnection(edge)}
            className="w-full text-xs border border-border bg-foreground text-background py-2 hover:opacity-90 transition-opacity"
          >
            Use this connection
          </button>
          <p className="text-[10px] text-muted-foreground leading-relaxed">
            Generates a warm-path opener referencing this evidence.
          </p>
        </section>
      )}
    </aside>
  );
}

// ── Tiny factor cell ────────────────────────────────────────────────────────

function FactorCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-card p-2 space-y-0.5">
      <div className="text-[9px] uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </div>
      <div className="text-mono text-xs">{value}</div>
    </div>
  );
}

