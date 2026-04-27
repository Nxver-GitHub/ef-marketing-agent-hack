/**
 * NodeInspector — right-rail context panel for the v2 Discover graph.
 *
 * Renders one of four primary variants (person / company / role / city) plus
 * a minimal default for school / conference / industry. Pure presentational:
 * it does NOT fetch — the parent page hands in the prospect/score/signals
 * blob for the person variant. Other variants are still placeholder-heavy
 * (firmographics, holder counts, candidate density) until those data feeds
 * land — search for "TODO(real-data)" below to find the seams.
 */
import type { JSX } from "react";
import { useMemo, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import type { GraphNode } from "@/lib/graph";
import type { Prospect, Score, Signal, SignalWeight } from "@/lib/mockStore";
import { scoreColor } from "@/components/ScoreBar";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { breakdownScore, type SignalContribution, type SubScoreKey } from "@/lib/scoreMath";
import type { HubStats } from "@/lib/aggregations";

export interface NodeInspectorProps {
  node: GraphNode | null;
  onClose?: () => void;
  prospect?: Prospect;
  score?: Score;
  signals?: Signal[];
  weights?: SignalWeight[];
  /** Live counts for non-person nodes (computed in Discover.tsx). */
  hubStats?: HubStats;
  /** Click-through from a hub's "top people" list into a person node. */
  onSelectProspect?: (prospectId: string) => void;
  onNavigateToProspect?: (id: string) => void;
}

// ── Local types ─────────────────────────────────────────────────────────────

interface SubScoreCell {
  label: string;
  value: string;
}

interface EvidenceRow {
  type: string;
  source: string;
  quote: string;
  confidence: number;
  timestamp: string;
  url?: string;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function bucketLabel(score: number): { label: string; tone: string } {
  if (score >= 90) return { label: "High-conviction", tone: "bg-score-strong/15 text-score-strong border-score-strong/30" };
  if (score >= 75) return { label: "Strong", tone: "bg-score-strong/10 text-score-strong border-score-strong/25" };
  if (score >= 60) return { label: "Plausible", tone: "bg-score-plausible/10 text-score-plausible border-score-plausible/30" };
  return { label: "Likely wrong", tone: "bg-score-weak/10 text-score-weak border-score-weak/30" };
}

function fmtTimestamp(ms: number): string {
  const days = Math.max(1, Math.round((Date.now() - ms) / 86_400_000));
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${Math.round(days / 365)}y ago`;
}

function quoteFromSignal(s: Signal): string {
  if (typeof s.value === "string") return s.value;
  if (typeof s.value === "number") return `${s.signal_type.replace(/_/g, " ")}: ${s.value}`;
  if (s.value && typeof s.value === "object" && "value" in s.value) {
    return `${s.signal_type.replace(/_/g, " ")}: ${(s.value as { value: unknown }).value}`;
  }
  return s.signal_type.replace(/_/g, " ");
}

// ── Sub-components ──────────────────────────────────────────────────────────

const Eyebrow = ({ children, className }: { children: React.ReactNode; className?: string }) => (
  <div className={cn("text-[10px] uppercase tracking-[0.18em] text-muted-foreground", className)}>
    {children}
  </div>
);

const SubScoreGrid = ({ cells }: { cells: SubScoreCell[] }) => (
  <div className="grid grid-cols-2 gap-px bg-border border border-border">
    {cells.map((c) => (
      <div key={c.label} className="bg-card p-3 space-y-1">
        <Eyebrow>{c.label}</Eyebrow>
        <div className="text-mono text-lg">{c.value}</div>
      </div>
    ))}
  </div>
);

const EvidenceList = ({ rows }: { rows: EvidenceRow[] }) => (
  <div className="space-y-3">
    {rows.map((row, i) => (
      <div key={i} className="border border-border bg-card p-3 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[9px] uppercase tracking-[0.16em] px-1.5 py-0.5 border border-border text-muted-foreground shrink-0">
              {row.type}
            </span>
            <span className="text-xs truncate">{row.source}</span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <span
              className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ background: scoreColor(row.confidence) }}
            />
            <span className="text-mono text-[11px] text-muted-foreground">
              {row.confidence.toFixed(0)}
            </span>
          </div>
        </div>
        <div className="text-xs text-foreground/85 leading-relaxed line-clamp-3">
          “{row.quote}”
        </div>
        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span className="text-mono">{row.timestamp}</span>
          <a
            href={row.url ?? "#"}
            target="_blank"
            rel="noreferrer"
            className="hover:text-foreground transition-colors"
          >
            Open source ↗
          </a>
        </div>
      </div>
    ))}
  </div>
);

const Avatar = ({ kind }: { kind: GraphNode["kind"] }) => {
  const baseSize = "w-11 h-11 shrink-0";
  switch (kind) {
    case "person":
      return <div className={cn(baseSize, "rounded-full bg-node-person border border-border")} />;
    case "company":
      return <div className={cn(baseSize, "rounded-md bg-node-company border border-border")} />;
    case "role":
      return (
        <div
          className={cn(baseSize, "bg-node-role border border-border")}
          style={{ clipPath: "polygon(25% 5%, 75% 5%, 100% 50%, 75% 95%, 25% 95%, 0% 50%)" }}
        />
      );
    case "city":
      return <div className="w-[54px] h-8 rounded-full bg-node-city border border-border shrink-0" />;
    case "school":
      return <div className={cn(baseSize, "bg-node-school border border-border")} style={{ clipPath: "polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)" }} />;
    case "conference":
      return <div className={cn(baseSize, "rounded-md rotate-45 bg-node-conference border border-border")} />;
    case "industry":
      return <div className={cn(baseSize, "rounded-sm bg-node-industry border border-border")} />;
  }
};

// ── Main ────────────────────────────────────────────────────────────────────

export function NodeInspector(props: NodeInspectorProps): JSX.Element | null {
  const {
    node,
    onClose,
    prospect,
    score,
    signals,
    weights,
    hubStats,
    onSelectProspect,
    onNavigateToProspect,
  } = props;
  if (!node) return null;

  const Header = (
    <div className="flex items-center justify-between mb-4">
      <Eyebrow>Selected node</Eyebrow>
      {onClose && (
        <button
          onClick={onClose}
          className="text-muted-foreground hover:text-foreground transition-colors p-1 -m-1"
          aria-label="Close inspector"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  );

  // ─── PERSON ────────────────────────────────────────────────────────────────
  if (node.kind === "person") {
    return (
      <PersonInspector
        node={node}
        Header={Header}
        prospect={prospect}
        score={score}
        signals={signals}
        weights={weights}
        onNavigateToProspect={onNavigateToProspect}
      />
    );
  }

  // ─── AGGREGATION (company / role / city / school / conference / industry) ─
  return (
    <AggregationInspector
      node={node}
      Header={Header}
      hubStats={hubStats}
      onSelectProspect={onSelectProspect}
    />
  );
}

// ── Layout primitives ───────────────────────────────────────────────────────

const PanelShell = ({ children }: { children: React.ReactNode }) => (
  <aside className="w-[380px] shrink-0 border-l border-border bg-background h-full overflow-y-auto p-5">
    {children}
  </aside>
);

const IdentityCard = ({
  avatar,
  name,
  subLine,
  bigValue,
  bigLabel,
  colored,
}: {
  avatar: React.ReactNode;
  name: string;
  subLine: string;
  bigValue: number;
  bigLabel: string;
  colored?: boolean;
}) => (
  <div className="flex items-start justify-between gap-4">
    <div className="flex items-start gap-3 min-w-0">
      {avatar}
      <div className="min-w-0 pt-0.5">
        <div className="text-base font-medium truncate">{name}</div>
        <div className="text-xs text-muted-foreground mt-1 line-clamp-2">{subLine}</div>
      </div>
    </div>
    <div className="text-right shrink-0">
      <div
        className="text-mono text-3xl leading-none"
        style={colored ? { color: scoreColor(bigValue) } : undefined}
      >
        {Math.round(bigValue)}
      </div>
      <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground mt-1">
        {bigLabel}
      </div>
    </div>
  </div>
);

const EvidenceHead = ({ title, count }: { title: string; count: number }) => (
  <div className="flex items-baseline justify-between mt-5 mb-2">
    <Eyebrow>{title}</Eyebrow>
    <span className="text-mono text-[11px] text-muted-foreground">{count}</span>
  </div>
);

// ── Person variant — surfaces the actual scoring math ───────────────────────

const SUB_SCORE_LABEL: Record<SubScoreKey, string> = {
  authenticity: "Authenticity",
  authority: "Authority",
  warmth: "Warmth",
};

interface PersonInspectorProps {
  node: GraphNode & { kind: "person" };
  Header: JSX.Element;
  prospect?: Prospect;
  score?: Score;
  signals?: Signal[];
  weights?: SignalWeight[];
  onNavigateToProspect?: (id: string) => void;
}

function PersonInspector({
  node,
  Header,
  prospect,
  score,
  signals,
  weights,
  onNavigateToProspect,
}: PersonInspectorProps): JSX.Element {
  // Re-derive from raw signals so the breakdown numbers match what the user
  // sees in the totals. If we don't have signals/weights yet, fall back to
  // the persisted score totals only.
  const breakdown = useMemo(() => {
    if (!signals || signals.length === 0 || !weights || weights.length === 0) return null;
    return breakdownScore(signals, weights);
  }, [signals, weights]);

  const overall = score?.overall_score ?? breakdown?.subScores.overall ?? node.score ?? 0;
  const authenticity = score?.authenticity_score ?? breakdown?.subScores.authenticity ?? 0;
  const authority = score?.authority_score ?? breakdown?.subScores.authority ?? 0;
  const warmth = score?.warmth_score ?? breakdown?.subScores.warmth ?? 0;
  const evidenceCount = signals?.length ?? 0;
  const bucket = bucketLabel(overall);
  const cityLine =
    (node.raw as Prospect & { industry?: string }).industry ?? prospect?.industry ?? "";
  const subLine = prospect
    ? `${prospect.role} · ${prospect.company}${cityLine ? ` · ${cityLine}` : ""}`
    : `${node.role}${cityLine ? ` · ${cityLine}` : ""}`;

  return (
    <PanelShell>
      {Header}
      <IdentityCard
        avatar={<Avatar kind="person" />}
        name={prospect?.name ?? node.name}
        subLine={subLine}
        bigValue={overall}
        bigLabel="Overall"
        colored
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className={cn("inline-flex text-[10px] uppercase tracking-[0.16em] px-2 py-0.5 border", bucket.tone)}>
          {bucket.label}
        </span>
        {score?.computed_at && (
          <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
            Scored {fmtTimestamp(score.computed_at)}
          </span>
        )}
      </div>
      <Separator className="my-4" />
      <SubScoreGrid
        cells={[
          { label: "Authenticity", value: authenticity.toFixed(1) },
          { label: "Authority", value: authority.toFixed(1) },
          { label: "Warmth", value: warmth.toFixed(1) },
          { label: "Evidence", value: String(evidenceCount) },
        ]}
      />

      {breakdown ? (
        <BreakdownSections
          breakdown={breakdown}
          signals={signals ?? []}
          persistedSubScores={{
            authenticity: score?.authenticity_score,
            authority: score?.authority_score,
            warmth: score?.warmth_score,
          }}
        />
      ) : (
        <>
          <EvidenceHead title="Evidence trail" count={evidenceCount} />
          {evidenceCount > 0 ? (
            <EvidenceList
              rows={(signals ?? []).slice(0, 6).map((s) => ({
                type: s.signal_type.replace(/_/g, " "),
                source: s.source,
                quote: quoteFromSignal(s),
                confidence: Math.round((s.confidence ?? 0.7) * 100),
                timestamp: fmtTimestamp(s.collected_at),
              }))}
            />
          ) : (
            <p className="text-xs text-muted-foreground">
              No signals collected yet — score rolls up to 0 by default.
            </p>
          )}
        </>
      )}

      {score?.falsification_notes && score.falsification_notes.length > 0 && (
        <FalsificationBlock notes={score.falsification_notes} />
      )}

      {prospect && onNavigateToProspect && (
        <Button
          variant="outline"
          className="w-full mt-4"
          onClick={() => onNavigateToProspect(prospect._id)}
        >
          Open full profile ↗
        </Button>
      )}
    </PanelShell>
  );
}

function BreakdownSections({
  breakdown,
  signals,
  persistedSubScores,
}: {
  breakdown: NonNullable<ReturnType<typeof breakdownScore>>;
  signals: Signal[];
  persistedSubScores?: { authenticity?: number; authority?: number; warmth?: number };
}): JSX.Element {
  const signalById = useMemo(() => {
    const m = new Map<string, Signal>();
    for (const s of signals) m.set(s._id, s);
    return m;
  }, [signals]);

  return (
    <>
      <div className="mt-5 mb-2 flex items-baseline justify-between">
        <Eyebrow>How the score was built</Eyebrow>
        <span className="text-[10px] text-muted-foreground">click to expand</span>
      </div>
      <div className="space-y-2">
        {(Object.keys(SUB_SCORE_LABEL) as SubScoreKey[]).map((key) => {
          // Prefer the persisted sub-score (server scorer) — the local
          // breakdown denominator goes to 0 when a prospect's signal_types
          // aren't in the snapshot's signal_weights table, but the persisted
          // value is correct in either case.
          const localValue = breakdown.subScores[key];
          const persisted = persistedSubScores?.[key];
          const value = (localValue && localValue > 0)
            ? localValue
            : (typeof persisted === "number" ? persisted : localValue);
          return (
            <BreakdownGroup
              key={key}
              label={SUB_SCORE_LABEL[key]}
              value={value}
              contributions={breakdown[key]}
              signalById={signalById}
            />
          );
        })}
      </div>
    </>
  );
}

function BreakdownGroup({
  label,
  value,
  contributions,
  signalById,
}: {
  label: string;
  value: number;
  contributions: SignalContribution[];
  signalById: Map<string, Signal>;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const top = contributions.slice(0, 5);
  const driver = top[0];

  return (
    <div className="border border-border bg-card">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-muted/40"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
            {label}
          </span>
          <span
            className="text-mono text-sm shrink-0"
            style={{ color: scoreColor(value) }}
          >
            {value.toFixed(1)}
          </span>
          {driver && (
            <span className="text-[11px] text-muted-foreground truncate">
              · top: {driver.signal_type.replace(/_/g, " ")} ({driver.pctOfSubScore.toFixed(0)}%)
            </span>
          )}
        </div>
        <ChevronDown
          className={cn(
            "w-3.5 h-3.5 text-muted-foreground shrink-0 transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open && (
        <div className="border-t border-border divide-y divide-border">
          {top.length === 0 && (
            <div className="px-3 py-3 text-[11px] text-muted-foreground">
              No signals contribute to this sub-score yet.
            </div>
          )}
          {top.map((c) => {
            const s = signalById.get(c.signalId);
            return (
              <div key={c.signalId} className="px-3 py-2 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground truncate">
                    {c.signal_type.replace(/_/g, " ")}
                  </span>
                  <span className="text-mono text-[11px] shrink-0">
                    {c.pctOfSubScore.toFixed(0)}%
                  </span>
                </div>
                <div className="h-1 bg-muted overflow-hidden">
                  <div
                    className="h-full bg-foreground/70"
                    style={{ width: `${Math.min(100, c.pctOfSubScore)}%` }}
                  />
                </div>
                <div className="flex items-center justify-between text-[10px] text-muted-foreground">
                  <span className="truncate">
                    {c.source} · norm {c.normalized.toFixed(0)} × conf{" "}
                    {(c.confidence * 100).toFixed(0)}% × w {c.subWeight.toFixed(2)}
                  </span>
                  {s?.collected_at && (
                    <span className="text-mono shrink-0 ml-2">
                      {fmtTimestamp(s.collected_at)}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
          {contributions.length > top.length && (
            <div className="px-3 py-2 text-[11px] text-muted-foreground">
              + {contributions.length - top.length} more lower-impact signals
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FalsificationBlock({ notes }: { notes: string[] }): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-5">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 border border-border bg-card hover:bg-muted/40"
      >
        <div className="flex items-center gap-2 min-w-0">
          <Eyebrow>Could be wrong if…</Eyebrow>
          <span className="text-mono text-[11px] text-muted-foreground">{notes.length}</span>
        </div>
        <ChevronDown
          className={cn(
            "w-3.5 h-3.5 text-muted-foreground shrink-0 transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open && (
        <ul className="mt-2 space-y-2">
          {notes.map((n, i) => (
            <li
              key={i}
              className="text-[11px] text-foreground/80 border-l-2 border-score-weak/40 pl-3 leading-relaxed"
            >
              {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Aggregation variant — live counts, no fake firmographics ────────────────

const KIND_NOUN: Record<Exclude<GraphNode["kind"], "person">, [string, string]> = {
  company: ["employee", "employees in network"],
  role: ["holder", "people in this role"],
  city: ["candidate", "people based here"],
  school: ["alum", "alumni in network"],
  conference: ["speaker", "people who spoke here"],
  industry: ["operator", "people in this industry"],
};

interface AggregationInspectorProps {
  node: Exclude<GraphNode, { kind: "person" }>;
  Header: JSX.Element;
  hubStats?: HubStats;
  onSelectProspect?: (prospectId: string) => void;
}

function AggregationInspector({
  node,
  Header,
  hubStats,
  onSelectProspect,
}: AggregationInspectorProps): JSX.Element {
  const noun = KIND_NOUN[node.kind] ?? ["person", "in network"];
  const total = hubStats?.total ?? 0;
  const subLine = (() => {
    switch (node.kind) {
      case "company":
        // The graph builder hangs a city + industry off the company node when
        // it can resolve them (COMPANY_META). Surface that — it's real data.
        return [
          (node as Extract<GraphNode, { kind: "company" }>).industryId?.replace(
            "industry:",
            "",
          ),
          (node as Extract<GraphNode, { kind: "company" }>).locationId?.replace("city:", ""),
        ]
          .filter(Boolean)
          .join(" · ") || `Company · ${total} ${noun[0]}${total === 1 ? "" : "s"}`;
      case "role":
        return `Canonical role · ${total} ${noun[0]}${total === 1 ? "" : "s"}`;
      case "city":
        return (node as Extract<GraphNode, { kind: "city" }>).country
          ? `${(node as Extract<GraphNode, { kind: "city" }>).country} · ${total} ${noun[0]}${
              total === 1 ? "" : "s"
            }`
          : `${total} ${noun[0]}${total === 1 ? "" : "s"}`;
      case "school":
        return `School · ${total} ${noun[0]}${total === 1 ? "" : "s"}`;
      case "conference":
        return `Conference · ${total} speaker${total === 1 ? "" : "s"}`;
      case "industry":
        return `Industry · ${total} ${noun[0]}${total === 1 ? "" : "s"}`;
      default:
        return `${total} in network`;
    }
  })();

  return (
    <PanelShell>
      {Header}
      <IdentityCard
        avatar={<Avatar kind={node.kind} />}
        name={node.name}
        subLine={subLine}
        bigValue={total}
        bigLabel={noun[0] + (total === 1 ? "" : "s")}
      />
      <Separator className="my-4" />
      {hubStats ? (
        <>
          <SubScoreGrid
            cells={[
              { label: "In network", value: String(hubStats.total) },
              { label: "Avg score", value: hubStats.avgScore.toFixed(1) },
              { label: "High-conf", value: String(hubStats.highConf) },
              {
                label: "Top role",
                value: hubStats.topRoles[0]?.label?.split(" ").slice(0, 2).join(" ") ?? "—",
              },
            ]}
          />
          {hubStats.topRoles.length > 0 && (
            <TallyBlock title="Top roles" rows={hubStats.topRoles} total={hubStats.total} />
          )}
          {hubStats.topIndustries.length > 0 && node.kind !== "industry" && (
            <TallyBlock
              title="Top industries"
              rows={hubStats.topIndustries}
              total={hubStats.total}
            />
          )}
          {hubStats.topPeople.length > 0 && (
            <>
              <EvidenceHead title="Highest-scoring people" count={hubStats.topPeople.length} />
              <ul className="border border-border bg-card divide-y divide-border">
                {hubStats.topPeople.map((p) => (
                  <li key={p.id}>
                    <button
                      onClick={() => onSelectProspect?.(p.id)}
                      className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-muted/40"
                      disabled={!onSelectProspect}
                    >
                      <div className="min-w-0">
                        <div className="text-xs truncate">{p.name}</div>
                        <div className="text-[10px] text-muted-foreground truncate">
                          {p.role}
                        </div>
                      </div>
                      <span
                        className="text-mono text-sm shrink-0"
                        style={{ color: scoreColor(p.score) }}
                      >
                        {Math.round(p.score)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      ) : (
        <div className="text-xs text-muted-foreground">
          No connected prospects in the rendered graph.
        </div>
      )}
    </PanelShell>
  );
}

function TallyBlock({
  title,
  rows,
  total,
}: {
  title: string;
  rows: { label: string; count: number }[];
  total: number;
}): JSX.Element {
  return (
    <>
      <EvidenceHead title={title} count={rows.length} />
      <ul className="space-y-1.5">
        {rows.map((r) => {
          const pct = total > 0 ? (r.count / total) * 100 : 0;
          return (
            <li key={r.label} className="space-y-0.5">
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="truncate">{r.label}</span>
                <span className="text-mono text-muted-foreground shrink-0">
                  {r.count}
                </span>
              </div>
              <div className="h-1 bg-muted overflow-hidden">
                <div
                  className="h-full bg-foreground/60"
                  style={{ width: `${Math.min(100, pct)}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}
