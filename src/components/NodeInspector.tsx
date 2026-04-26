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
import { X } from "lucide-react";
import type { GraphNode } from "@/lib/graph";
import type { Prospect, Score, Signal } from "@/lib/mockStore";
import { scoreColor } from "@/components/ScoreBar";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface NodeInspectorProps {
  node: GraphNode | null;
  onClose?: () => void;
  prospect?: Prospect;
  score?: Score;
  signals?: Signal[];
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
  const { node, onClose, prospect, score, signals, onNavigateToProspect } = props;
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
    const overall = score?.overall_score ?? node.score ?? 0;
    const bucket = bucketLabel(overall);
    const cityLine =
      (node.raw as Prospect & { industry?: string }).industry ?? prospect?.industry ?? "";
    const subLine = prospect
      ? `${prospect.role} · ${prospect.company}${cityLine ? ` · ${cityLine}` : ""}`
      : `${node.role}${cityLine ? ` · ${cityLine}` : ""}`;

    const evidence: EvidenceRow[] =
      signals && signals.length > 0
        ? signals.slice(0, 6).map((s) => ({
            type: s.signal_type.replace(/_/g, " "),
            source: s.source,
            quote: quoteFromSignal(s),
            confidence: Math.round((s.confidence ?? 0.7) * 100),
            timestamp: fmtTimestamp(s.collected_at),
          }))
        : [
            {
              type: "Job description",
              source: "Greenhouse · VP Process Eng req",
              quote: "Owns yield ramp for 3nm node; cross-functional authority over fab integration.",
              confidence: 88,
              timestamp: "12d ago",
            },
            {
              type: "Earnings transcript",
              source: "TSMC Q3 2024 call",
              quote: "Lin's team delivered the N3 yield improvement that drove our gross margin recovery.",
              confidence: 92,
              timestamp: "2mo ago",
            },
            {
              type: "Patent",
              source: "USPTO · Granted 2024",
              quote: "Method and apparatus for GAA transistor yield optimization — first inventor.",
              confidence: 84,
              timestamp: "5mo ago",
            },
            {
              type: "Press release",
              source: "TSMC Newsroom",
              quote: "Promoted to VP, Process Engineering, leading 3nm and 2nm technology development.",
              confidence: 78,
              timestamp: "8mo ago",
            },
          ];

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
        <div className="mt-3">
          <span className={cn("inline-flex text-[10px] uppercase tracking-[0.16em] px-2 py-0.5 border", bucket.tone)}>
            {bucket.label}
          </span>
        </div>
        <Separator className="my-4" />
        <SubScoreGrid
          cells={[
            { label: "Authenticity", value: (score?.authenticity_score ?? 0).toFixed(1) },
            { label: "Authority", value: (score?.authority_score ?? 0).toFixed(1) },
            { label: "Warmth", value: (score?.warmth_score ?? 0).toFixed(1) },
            { label: "Confidence", value: (score?.overall_score ?? 0).toFixed(1) },
          ]}
        />
        <EvidenceHead title="Evidence trail" count={evidence.length} />
        <EvidenceList rows={evidence} />
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

  // ─── COMPANY ───────────────────────────────────────────────────────────────
  if (node.kind === "company") {
    // TODO(real-data): firmographics + ICP-fit numbers should come from props
    // once the company-enrichment slice lands. Today: hardcoded sample.
    const evidence: EvidenceRow[] = [
      { type: "ATS", source: "Greenhouse · 8 open VP+ reqs", quote: "Aggressive senior hiring across process, packaging, design verification.", confidence: 86, timestamp: "3d ago" },
      { type: "SEC filing", source: "S-1 · 2024-Q3", quote: "Capacity expansion guidance up 18% YoY; capex weighted toward advanced node.", confidence: 91, timestamp: "1mo ago" },
      { type: "Press", source: "Reuters · Strategic partnership", quote: "Multi-year supply agreement signed with major US hyperscaler.", confidence: 79, timestamp: "2mo ago" },
      { type: "LinkedIn", source: "VP-level moves", quote: "Net +6 VP-level hires past 90d; concentrated in NPI and yield orgs.", confidence: 72, timestamp: "1w ago" },
    ];
    return (
      <PanelShell>
        {Header}
        <IdentityCard
          avatar={<Avatar kind="company" />}
          name={node.name}
          subLine="Series D · Semiconductors · Hsinchu · ~70k emp"
          bigValue={88}
          bigLabel="ICP fit"
        />
        <div className="mt-3">
          <Badge variant="outline" className="text-[10px] uppercase tracking-[0.16em] font-normal">
            ICP match · 7 open VP+ reqs · hiring velocity ↑
          </Badge>
        </div>
        <Separator className="my-4" />
        <SubScoreGrid
          cells={[
            { label: "ICP fit", value: "88" },
            { label: "Hiring vel.", value: "74" },
            { label: "Tech maturity", value: "91" },
            { label: "Org density", value: "12" },
          ]}
        />
        <EvidenceHead title="Company evidence" count={evidence.length} />
        <EvidenceList rows={evidence} />
      </PanelShell>
    );
  }

  // ─── ROLE ──────────────────────────────────────────────────────────────────
  if (node.kind === "role") {
    // TODO(real-data): holder count + avg authority/tenure need a graph-side
    // aggregation pass. Placeholders for now.
    const evidence: EvidenceRow[] = [
      { type: "JD title", source: "Greenhouse · Aggregate", quote: "Title appears across 14 active reqs at peer ICP companies.", confidence: 80, timestamp: "1w ago" },
      { type: "Scope signal", source: "LinkedIn bios", quote: "Holders cite P&L authority and direct reports between 40–120.", confidence: 76, timestamp: "2w ago" },
      { type: "Reports graph", source: "Org rollup", quote: "Median holder has 4 directs at staff/principal grade.", confidence: 71, timestamp: "1mo ago" },
      { type: "Promotion patterns", source: "Career trajectory", quote: "Common predecessor roles: Sr Director Process, Distinguished Engineer.", confidence: 68, timestamp: "3mo ago" },
    ];
    return (
      <PanelShell>
        {Header}
        <IdentityCard
          avatar={<Avatar kind="role" />}
          name={node.name}
          subLine="Target role · Inferred from scope signals & JD parsing"
          bigValue={12}
          bigLabel="Holders"
        />
        <div className="mt-3">
          <Badge variant="outline" className="text-[10px] uppercase tracking-[0.16em] font-normal">
            12 inferred holders · 5 high-confidence
          </Badge>
        </div>
        <Separator className="my-4" />
        <SubScoreGrid
          cells={[
            { label: "Holders", value: "12" },
            { label: "High conf.", value: "5" },
            { label: "Avg auth.", value: "76.4" },
            { label: "Avg tenure", value: "4.2y" },
          ]}
        />
        <EvidenceHead title="Role evidence" count={evidence.length} />
        <EvidenceList rows={evidence} />
      </PanelShell>
    );
  }

  // ─── CITY ──────────────────────────────────────────────────────────────────
  if (node.kind === "city") {
    // TODO(real-data): candidate count + density should be derived from the
    // graph (count of person nodes whose company.locationId === this city).
    const evidence: EvidenceRow[] = [
      { type: "Crunchbase", source: "HQ density", quote: "31 ICP-tier semiconductor companies headquartered in metro.", confidence: 88, timestamp: "1w ago" },
      { type: "ATS", source: "Greenhouse · local jobs", quote: "210 open senior+ reqs across target functions in past 30d.", confidence: 82, timestamp: "5d ago" },
      { type: "LinkedIn", source: "Density estimate", quote: "Estimated 1.2k VP-level professionals in target verticals.", confidence: 70, timestamp: "2w ago" },
      { type: "News", source: "Hiring trend", quote: "Net positive senior migration past 4 quarters; +8% YoY.", confidence: 74, timestamp: "1mo ago" },
    ];
    return (
      <PanelShell>
        {Header}
        <IdentityCard
          avatar={<Avatar kind="city" />}
          name={node.name}
          subLine={`Metro · ${node.country ?? "Region"} · 31 ICP companies`}
          bigValue={87}
          bigLabel="Candidates"
        />
        <div className="mt-3">
          <Badge variant="outline" className="text-[10px] uppercase tracking-[0.16em] font-normal">
            High density · 31 cos. in network · avg score 74
          </Badge>
        </div>
        <Separator className="my-4" />
        <SubScoreGrid
          cells={[
            { label: "Candidates", value: "87" },
            { label: "Companies", value: "31" },
            { label: "Avg score", value: "74.1" },
            { label: "Density", value: "high" },
          ]}
        />
        <EvidenceHead title="City evidence" count={evidence.length} />
        <EvidenceList rows={evidence} />
      </PanelShell>
    );
  }

  // ─── DEFAULT (school / conference / industry / unknown) ────────────────────
  return (
    <PanelShell>
      {Header}
      <div className="flex items-center gap-3">
        <Avatar kind={node.kind} />
        <div className="min-w-0">
          <div className="text-base truncate">{node.name}</div>
          <Eyebrow className="mt-1">{node.kind}</Eyebrow>
        </div>
      </div>
      <Separator className="my-4" />
      <div className="text-xs text-muted-foreground">
        More signals coming soon for {node.kind} nodes.
      </div>
    </PanelShell>
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
