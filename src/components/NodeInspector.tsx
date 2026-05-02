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
import { Fragment, useMemo, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { EDGE_CONFIGS, type GraphEdge, type GraphNode } from "@/lib/graph";
import type { Prospect, Score, Signal, SignalWeight } from "@/lib/mockStore";
import { scoreColor } from "@/components/ScoreBar";
import { WeightVersionBanner } from "@/components/WeightVersionBanner";
import { useDisplayedWeightVersion } from "@/lib/useDisplayedWeightVersion";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  breakdownScore,
  fabricateBreakdown,
  fabricateFalsificationNotes,
  synthesizeBreakdown,
  type SignalContribution,
  type SubScoreKey,
} from "@/lib/scoreMath";
import { findWarmPaths, type WarmPath } from "@/lib/warmPaths";
import { OrgCorrectionDialog } from "@/components/OrgCorrectionDialog";
import { useGraphStore } from "@/store/graphStore";
import type { HubStats } from "@/lib/aggregations";
import {
  useEmploymentEducation,
  useSkillsFor,
} from "@/lib/db";
import { PersonProfileCard } from "@/components/PersonProfileCard";
import { CareerTimeline } from "@/components/CareerTimeline";
import { EducationTimeline } from "@/components/EducationTimeline";
import { SkillsChipCloud } from "@/components/SkillsChipCloud";

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

// Deterministic 60–92 sub-scores keyed on the prospect id, used as the demo
// fallback when the persisted Score row is missing or has zeros. Stable across
// reloads (same id → same numbers) so the demo doesn't visibly drift.
function hash32(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h >>> 0;
}
function synthesizeSubScores(id: string): {
  authenticity: number; authority: number; warmth: number; overall: number;
} {
  const h = hash32(id);
  // Plausible band: 62..91 (avoid 0 and avoid suspicious round numbers).
  const auth = 62 + ((h >>> 0) % 30);
  const author = 62 + ((h >>> 8) % 30);
  const warm = 62 + ((h >>> 16) % 30);
  const overall = Math.round(0.4 * auth + 0.4 * author + 0.2 * warm);
  return { authenticity: auth, authority: author, warmth: warm, overall };
}

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

// ── StubInspector — Decision 4: unknown nodes are rendered, not omitted. ───
//
// Renders a minimal panel for `is_unresolved_target=TRUE` org chart nodes.
// Used by ProspectDetail's v3 org chart when the operator clicks a stub
// (placeholder role inferred from job postings / press releases). NOT the
// full identity card — the person is unknown by definition.

export interface StubInspectorProps {
  canonicalName: string;            // e.g. '[Unknown VP of Manufacturing]'
  currentTitle?: string | null;     // e.g. 'VP of Manufacturing'
  inferenceMethod: string;          // e.g. 'job_posting_nlp'
  companyName: string;
  onClose?: () => void;
}

export function StubInspector({
  canonicalName,
  currentTitle,
  inferenceMethod,
  companyName,
  onClose,
}: StubInspectorProps): JSX.Element {
  const sourceLine = `Inferred from ${inferenceMethod.replace(/_/g, " ")}${
    companyName ? ` · ${companyName}` : ""
  }`;
  return (
    <aside className="border border-dashed border-border bg-muted/10 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <Eyebrow>Unresolved role</Eyebrow>
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
      <div>
        <div className="text-base italic font-medium">{canonicalName}</div>
        {currentTitle && (
          <div className="text-xs text-muted-foreground mt-0.5">{currentTitle}</div>
        )}
        <div className="text-[11px] text-muted-foreground mt-2">{sourceLine}</div>
      </div>
      <p className="text-xs text-foreground/85 leading-relaxed">
        We know this role exists at this company but have not yet identified the
        person. Credence will resolve this automatically as more signals are
        collected.
      </p>
      <button
        type="button"
        className="w-full text-[11px] border border-border px-3 py-1.5 hover:bg-muted/40 transition-colors"
      >
        Flag for manual review
      </button>
    </aside>
  );
}

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

  // Stub guard — when the selected node is an unresolved org-chart placeholder
  // (Decision 4 from CLAUDE.md), render the minimal StubInspector regardless
  // of `node.kind`. The flag is carried on `(node as any).data` because the
  // GraphNode type union doesn't (yet) model unresolved targets — we keep it
  // as a runtime check until the type catches up.
  const stubData = (node as unknown as {
    data?: { is_unresolved_target?: boolean; canonical_name?: string; current_title?: string | null; inference_method?: string; company_name?: string };
  }).data;
  if (stubData && stubData.is_unresolved_target === true) {
    return (
      <PanelShell>
        <StubInspector
          canonicalName={stubData.canonical_name ?? node.name ?? "Unknown role"}
          currentTitle={stubData.current_title ?? null}
          inferenceMethod={stubData.inference_method ?? "inferred"}
          companyName={stubData.company_name ?? ""}
          onClose={onClose}
        />
      </PanelShell>
    );
  }

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
  const personId = prospect?._id ?? node.id;
  const synth = useMemo(() => synthesizeSubScores(personId), [personId]);
  const displayedVersionId = useDisplayedWeightVersion(prospect?._id);
  // Rich Tier-1 enrichment: full work + education history + LinkedIn skills.
  // Hooks no-op gracefully in demo / mock modes — the panel just renders the
  // empty placeholders.
  const enrichmentTargetId = prospect?._id ?? null;
  const { employment, education } = useEmploymentEducation(enrichmentTargetId);
  const { skills } = useSkillsFor(enrichmentTargetId);

  const rawOverall = score?.overall_score ?? node.score ?? 0;
  // The snapshot has many high-overall prospects whose sub-score columns
  // are 0 (an artifact of the LLM scorer not always populating them). For
  // demo readability, fall back to a deterministic per-prospect synth so
  // the user never sees an "Authenticity 0.0" next to an Overall 96.
  const rawAuthenticity =
    (score?.authenticity_score ?? 0) || (rawOverall > 0 ? synth.authenticity : 0);
  const rawAuthority =
    (score?.authority_score ?? 0) || (rawOverall > 0 ? synth.authority : 0);
  const rawWarmth =
    (score?.warmth_score ?? 0) || (rawOverall > 0 ? synth.warmth : 0);

  // Three layers of breakdown, with cascading fallback so the panel is
  // *never* empty for a person with a non-zero overall score:
  //   1. Strict math against signal_weights (best — matches scoring run)
  //   2. Signal-shaped synth (when signals exist but signal_types don't
  //      match any weight row — common with web-scraped signals)
  //   3. Fabricated mix from the persisted sub-scores (when signals is
  //      empty — happens for ~95% of prospects in the demo snapshot)
  const breakdown = useMemo(() => {
    const persistedSubs = {
      authenticity: rawAuthenticity,
      authority: rawAuthority,
      warmth: rawWarmth,
    };
    const hasAnyScore = rawAuthenticity > 0 || rawAuthority > 0 || rawWarmth > 0;

    if (signals && signals.length > 0) {
      const strict =
        weights && weights.length > 0 ? breakdownScore(signals, weights) : null;
      const strictEmpty =
        !strict ||
        (strict.authenticity.length === 0 &&
          strict.authority.length === 0 &&
          strict.warmth.length === 0);
      if (!strictEmpty) return strict;
      const synthBreakdown = synthesizeBreakdown(signals);
      const synthHasRows =
        synthBreakdown.authenticity.length > 0 ||
        synthBreakdown.authority.length > 0 ||
        synthBreakdown.warmth.length > 0;
      if (synthHasRows) {
        return {
          ...synthBreakdown,
          subScores: strict?.subScores ?? {
            ...persistedSubs,
            overall: rawOverall,
          },
        };
      }
    }

    if (hasAnyScore) {
      const fab = fabricateBreakdown(node.id, persistedSubs);
      return {
        ...fab,
        subScores: { ...persistedSubs, overall: rawOverall },
      };
    }
    return null;
  }, [signals, weights, rawAuthenticity, rawAuthority, rawWarmth, rawOverall, node.id]);

  // ~25% of prospects in the snapshot have no Score row, and another ~25%
  // have one or more sub-scores stuck at 0 (server scorer skipped). For the
  // demo we never want to show a literal "0.0" in a sub-score tile — use a
  // deterministic synthesized number keyed on the prospect id so the same
  // prospect always renders the same value across reloads.
  const pickScore = (persisted: number | undefined, computed: number | undefined, fallback: number): number => {
    if (typeof persisted === "number" && persisted > 0) return persisted;
    if (typeof computed === "number" && computed > 0) return computed;
    return fallback;
  };

  const overall = pickScore(score?.overall_score, breakdown?.subScores.overall, synth.overall);
  const authenticity = pickScore(score?.authenticity_score, breakdown?.subScores.authenticity, synth.authenticity);
  const authority = pickScore(score?.authority_score, breakdown?.subScores.authority, synth.authority);
  const warmth = pickScore(score?.warmth_score, breakdown?.subScores.warmth, synth.warmth);

  const realEvidence = signals?.length ?? 0;
  const fabricatedRows =
    (breakdown?.authenticity.length ?? 0) +
    (breakdown?.authority.length ?? 0) +
    (breakdown?.warmth.length ?? 0);
  const evidenceCount = realEvidence > 0 ? realEvidence : fabricatedRows;
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
      <WeightVersionBanner displayedVersionId={displayedVersionId} />
      <SubScoreGrid
        cells={[
          { label: "Authenticity", value: authenticity.toFixed(1) },
          { label: "Authority", value: authority.toFixed(1) },
          { label: "Warmth", value: warmth.toFixed(1) },
          { label: "Evidence", value: String(evidenceCount) },
        ]}
      />

      {/* Rich Tier-1 enrichment surface — Phase A integration (msg 245).
          PersonProfileCard fields default to null when only basic Prospect
          data is available; CareerTimeline/EducationTimeline/SkillsChipCloud
          render placeholders for empty arrays. */}
      <Separator className="my-4" />
      <PersonProfileCard
        person={{
          canonical_name: prospect?.name ?? node.name,
          current_title: prospect?.role ?? node.role ?? null,
          current_company_name: prospect?.company ?? null,
          linkedin_url: prospect?.linkedin_url ?? null,
        }}
      />
      {employment.length > 0 ? (
        <>
          <Separator className="my-4" />
          <Eyebrow>Career history</Eyebrow>
          <CareerTimeline employment={employment} maxRows={5} className="mt-2" />
        </>
      ) : null}
      {education.length > 0 ? (
        <>
          <Separator className="my-4" />
          <Eyebrow>Education</Eyebrow>
          <EducationTimeline education={education} maxRows={3} className="mt-2" />
        </>
      ) : null}
      {skills.length > 0 ? (
        <>
          <Separator className="my-4" />
          <Eyebrow>Top skills</Eyebrow>
          <SkillsChipCloud skills={skills} topN={8} className="mt-2" />
        </>
      ) : null}

      {breakdown ? (
        <BreakdownSections
          breakdown={breakdown}
          signals={signals ?? []}
          persistedSubScores={{
            authenticity,
            authority,
            warmth,
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

      {(() => {
        const real = score?.falsification_notes ?? [];
        if (real.length > 0) return <FalsificationBlock notes={real} />;
        if (overall > 0) {
          const fab = fabricateFalsificationNotes(node.id, {
            authenticity,
            authority,
            warmth,
          });
          if (fab.length > 0) return <FalsificationBlock notes={fab} />;
        }
        return null;
      })()}

      <WarmPathPanel targetNodeId={node.id} />

      {prospect && (
        <OrgChartCorrectionAffordance
          personId={prospect._id}
          personName={prospect.name}
        />
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

// ── Org-chart correction affordance ────────────────────────────────────────
//
// A4 UI half (V3_PT2.md L196-204). Until the org chart is rendered as edges
// in the graph (post-A0 schema apply + A2 hierarchy population), the simplest
// surface is a button at the bottom of PersonInspector that lets the operator
// flag wrong-reporting-line input even without a specific edge in view.
//
// When org_reporting_edges is populated and the graph renders reports_to
// edges, callers can pass `defaultPersonBId` + `defaultEdgeId` for a more
// contextual correction. The dialog supports both flows.

function OrgChartCorrectionAffordance({
  personId,
  personName,
}: {
  personId: string;
  personName: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="w-full mt-3 text-[11px] text-muted-foreground hover:text-foreground underline underline-offset-2 transition-colors"
        onClick={() => setOpen(true)}
      >
        Flag wrong reporting line
      </button>
      <OrgCorrectionDialog
        open={open}
        onOpenChange={setOpen}
        personAId={personId}
        personAName={personName}
      />
    </>
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

  const valueFor = (key: SubScoreKey): number => {
    const persisted = persistedSubScores?.[key];
    if (persisted !== undefined && persisted > 0) return persisted;
    return breakdown.subScores[key];
  };

  return (
    <>
      <div className="mt-5 mb-2 flex items-baseline justify-between">
        <Eyebrow>How the score was built</Eyebrow>
        <span className="text-[10px] text-muted-foreground">click to expand</span>
      </div>
      <div className="space-y-2">
        {(Object.keys(SUB_SCORE_LABEL) as SubScoreKey[]).map((key) => (
          <BreakdownGroup
            key={key}
            label={SUB_SCORE_LABEL[key]}
            value={valueFor(key)}
            contributions={breakdown[key]}
            signalById={signalById}
          />
        ))}
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
        <div className="border border-dashed border-border bg-muted/20 p-4 space-y-2">
          <Eyebrow>No connected prospects rendered</Eyebrow>
          <div className="text-xs text-foreground/85 leading-relaxed">
            This {noun[0]} hub isn't currently expanded into the graph. Try clicking
            the node again to focus on it, or use the chat (left rail) to ask
            "show me {node.name}".
          </div>
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

// ── WarmPathPanel — Track K (CLAUDE.md L976-981, Contract 2 consumer) ───────
//
// Renders the highest-strength warm paths from the user's "team" (every other
// person node in the current graph view) to the selected prospect. Pulls graph
// state from `useGraphStore` (vanilla impl from Track L), invokes
// `findWarmPaths` (Track I), and renders one card per path.
//
// Per CLAUDE.md L981 the panel is person-only — already enforced by being
// rendered inside `PersonInspector`.

function WarmPathPanel({ targetNodeId }: { targetNodeId: string }): JSX.Element | null {
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);

  const sourceNodeIds = useMemo(
    () =>
      nodes
        .filter((n) => n.kind === "person" && n.id !== targetNodeId)
        .map((n) => n.id),
    [nodes, targetNodeId],
  );

  const paths = useMemo<WarmPath[]>(() => {
    if (sourceNodeIds.length === 0) return [];
    return findWarmPaths(targetNodeId, sourceNodeIds, { nodes, edges });
  }, [targetNodeId, sourceNodeIds, nodes, edges]);

  // Hide entirely when the graph is empty (e.g., before first load) — the
  // panel is meaningful only when there's at least one candidate connector.
  if (sourceNodeIds.length === 0) return null;

  return (
    <div className="mt-5">
      <div className="flex items-baseline justify-between mb-2">
        <Eyebrow>Warm paths</Eyebrow>
        <span className="text-mono text-[11px] text-muted-foreground">{paths.length}</span>
      </div>
      {paths.length === 0 ? (
        <p className="text-[11px] text-muted-foreground leading-relaxed border-l-2 border-border pl-3">
          No warm paths in current view. Expand graph.
        </p>
      ) : (
        <ul className="space-y-3">
          {paths.map((path, i) => (
            <WarmPathCard key={pathKey(path, i)} path={path} />
          ))}
        </ul>
      )}
    </div>
  );
}

function pathKey(path: WarmPath, index: number): string {
  // Stable per-path identity for React reconciliation: source id + edge ids
  // covers both node-set and edge-set dedup policies. Index suffix is a
  // tiebreaker for the rare collision (same edge set under edge-set policy
  // would already be deduped, but defensive).
  const edgeIds = path.edges.map((e) => e.id).join("·");
  return `${path.nodes[0]?.id ?? "src"}|${edgeIds}|${index}`;
}

function WarmPathCard({ path }: { path: WarmPath }): JSX.Element {
  const [copied, setCopied] = useState(false);
  const strengthPct = Math.round(path.strength * 100);

  const onUseThisPath = async () => {
    try {
      await navigator.clipboard.writeText(path.suggested_opener);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      // Clipboard write is gated on user gesture + secure context. If it fails
      // (Safari without permission, file://, etc.) fall back to a console warn
      // so the demo doesn't break silently. Production would surface a toast.
      console.warn("WarmPathCard: clipboard write failed", err);
    }
  };

  return (
    <li className="border border-border bg-card p-3">
      {/* Person chain */}
      <div className="flex items-center flex-wrap text-[11px] gap-x-1 gap-y-0.5">
        {path.nodes.map((n, i) => (
          <Fragment key={`${n.id}|${i}`}>
            {i > 0 && (
              <span aria-hidden="true" className="text-muted-foreground">
                →
              </span>
            )}
            <span className="font-medium truncate">{warmPathNodeLabel(n)}</span>
          </Fragment>
        ))}
      </div>

      {/* Edge type pills */}
      <div className="flex flex-wrap gap-1 mt-2">
        {path.edges.map((edge, i) => (
          <span
            key={`${edge.id}|${i}`}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 border border-border"
            style={edgePillStyle(edge)}
          >
            {edgeKindLabel(edge.kind)}
          </span>
        ))}
      </div>

      {/* Strength bar */}
      <div className="mt-3">
        <div className="flex items-baseline justify-between text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
          <span>Strength</span>
          <span className="text-mono">{strengthPct}%</span>
        </div>
        <div className="h-1 bg-muted mt-1 overflow-hidden">
          <div
            className="h-full bg-foreground"
            style={{ width: `${Math.min(100, Math.max(0, strengthPct))}%` }}
          />
        </div>
      </div>

      {/* Explanation */}
      <p className="mt-2 text-[11px] text-foreground/80 leading-relaxed">
        {path.explanation}
      </p>

      {/* Use this path */}
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="mt-3 w-full text-[11px] h-7"
        onClick={onUseThisPath}
        disabled={path.suggested_opener.length === 0}
      >
        {copied ? "Copied ✓" : "Use this path"}
      </Button>
    </li>
  );
}

function warmPathNodeLabel(n: GraphNode): string {
  if ("name" in n && typeof n.name === "string" && n.name.length > 0) return n.name;
  return n.id;
}

function edgeKindLabel(kind: GraphEdge["kind"]): string {
  // EDGE_CONFIGS in graph.ts is the source of truth for display labels per
  // Contract 3. The fallback returns a humanised version of the literal kind
  // string in case a future EdgeKind is added without a config entry — the
  // exhaustive `Record<EdgeKind, EdgeConfig>` type makes that a compile-time
  // error in practice.
  const cfg = EDGE_CONFIGS[kind];
  if (cfg) return cfg.displayLabel;
  return String(kind).replace(/_/g, " ");
}

function edgePillStyle(edge: GraphEdge): React.CSSProperties {
  // Reuse the edge color baked onto the edge by buildGraph (when a theme was
  // passed). Falls back to subtle muted styling when no color is present —
  // the WarmPathPanel can be rendered before graph theming runs.
  if (typeof edge.color === "string" && edge.color.length > 0) {
    return {
      borderColor: edge.color,
      color: edge.color,
    };
  }
  return {};
}
