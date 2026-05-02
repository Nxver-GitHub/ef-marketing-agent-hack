import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import {
  db,
  useProspect,
  useSignalsFor,
  useLatestScore,
  useLatestRun,
  useEmploymentEducation,
  useSkillsFor,
  type ScoringRun,
} from "@/lib/db";
import { PersonProfileCard } from "@/components/PersonProfileCard";
import { CareerTimeline } from "@/components/CareerTimeline";
import { EducationTimeline } from "@/components/EducationTimeline";
import { SkillsChipCloud } from "@/components/SkillsChipCloud";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import { getCredenceHeaders } from "@/lib/credenceHeaders";
import { BigScore, ScoreBar, scoreColor } from "@/components/ScoreBar";
import { ENABLE_ORG_CHART, supabase } from "@/lib/supabase";
import { WebPresence } from "@/components/WebPresence";
import ReactFlow, {
  Background,
  Controls,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from "reactflow";
import "reactflow/dist/style.css";
import { StubInspector } from "@/components/NodeInspector";

// ─── Org chart helpers ──────────────────────────────────────────────────────
// Seniority rank derived from role-title tokens. Used to place peers around
// the target prospect: higher-rank = manager (above), same ±10 = peer, lower
// = direct report.
interface OrgPerson {
  id: string;
  name: string;
  role: string;
  company?: string;
  industry?: string;
  linkedin_url?: string;
  source?: "supabase" | "fastapi";
}

function seniorityRank(role: string | null | undefined): number {
  if (!role) return 40;
  const r = role.toLowerCase();
  if (/\b(ceo|cto|coo|cfo|chief\b|president|founder)\b/.test(r)) return 95;
  if (/\b(svp|senior vice president|evp|executive vp)\b/.test(r)) return 88;
  if (/\b(vp|vice president)\b/.test(r)) return 80;
  if (/\b(senior director|sr\.? director|head of)\b/.test(r)) return 72;
  if (/\b(director)\b/.test(r)) return 66;
  if (/\b(principal|staff|fellow|distinguished)\b/.test(r)) return 58;
  if (/\b(senior|sr\.?)\b/.test(r)) return 48;
  if (/\b(manager|lead)\b/.test(r)) return 44;
  return 36;
}

const ALL_SOURCES = [
  "linkedin_profile",
  "linkedin_posts",
  "uspto",
  "github",
  "conference",
  "company_hiring",
  "crunchbase",
  "mutual_connections",
];

const ProspectDetail = () => {
  const { id } = useParams();
  const prospect = useProspect(id);
  useDocumentTitle(prospect?.name ?? "Prospect");
  const signals = useSignalsFor(id);
  const score = useLatestScore(id);
  const run = useLatestRun(id);
  // Phase A6 mirror — pull rich Tier-1 enrichment for the standalone page.
  // Hooks no-op gracefully when the prospect→person link is missing.
  const { employment, education } = useEmploymentEducation(id ?? null);
  const { skills } = useSkillsFor(id ?? null);
  const [tab, setTab] = useState<"overview" | "org">("overview");
  const [showRaw, setShowRaw] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [signalQuery, setSignalQuery] = useState("");

  // Client-side substring filter over signal source / signal_type / value /
  // raw_data. Kept purely local — no DB round-trips.
  const filteredSignals = useMemo(() => {
    const q = signalQuery.trim().toLowerCase();
    if (!q) return signals;
    return signals.filter((s) => {
      const hay = [
        s.source,
        s.signal_type,
        String(s.value ?? ""),
        (() => {
          try {
            return typeof s.raw_data === "string"
              ? s.raw_data
              : JSON.stringify(s.raw_data ?? "");
          } catch {
            return "";
          }
        })(),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [signals, signalQuery]);

  if (!prospect) {
    return (
      <PageShell>
        <div className="grid md:grid-cols-12 gap-10 min-h-[40vh] items-start py-12">
          <div className="md:col-span-7 space-y-4">
            <div className="label-eyebrow">Prospect not found</div>
            <h1 className="text-3xl md:text-4xl font-light tracking-tight leading-tight">
              We couldn't find this person in the network.
            </h1>
            <p className="text-sm text-muted-foreground max-w-prose">
              The prospect may have been removed, the URL might be stale, or the
              snapshot you're running against doesn't include this id ({id?.slice(0, 8) ?? "?"}).
            </p>
            <div className="flex gap-3 pt-2">
              <Link
                to="/discover"
                className="inline-flex items-center gap-2 px-3 py-1.5 border border-border text-xs text-mono hover:bg-secondary"
              >
                ← back to network
              </Link>
              <Link
                to="/validate"
                className="inline-flex items-center gap-2 px-3 py-1.5 border border-border text-xs text-mono hover:bg-secondary"
              >
                Search a new prospect →
              </Link>
            </div>
          </div>
        </div>
      </PageShell>
    );
  }

  const inProgress = run && run.status !== "complete";
  const succeeded = new Set(run?.sources_succeeded ?? []);

  return (
    <PageShell>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <div className="label-eyebrow mb-2">Prospect</div>
          <h1 className="text-4xl md:text-5xl font-light tracking-tight">{prospect.name}</h1>
          <div className="text-sm text-muted-foreground mt-2">
            {prospect.role} · {prospect.company} · {prospect.industry}
          </div>
          {(prospect.linkedin_url || prospect.email) && (
            <div className="text-sm mt-3 flex flex-wrap gap-x-4 gap-y-1 items-center">
              {prospect.linkedin_url && (
                <a
                  href={prospect.linkedin_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-foreground underline underline-offset-4 hover:text-foreground/80"
                >
                  LinkedIn ↗
                </a>
              )}
              {prospect.email && (
                <a
                  href={`mailto:${prospect.email}`}
                  className="text-foreground underline underline-offset-4 hover:text-foreground/80"
                >
                  {prospect.email}
                </a>
              )}
            </div>
          )}
        </div>
        <Link to="/discover" className="text-xs text-mono text-muted-foreground hover:text-foreground">
          ← back
        </Link>
      </div>

      {ENABLE_ORG_CHART && score && (
        <div className="flex gap-6 border-b border-border mb-8">
          {(["overview", "org"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`pb-3 text-xs uppercase tracking-[0.16em] border-b-2 -mb-px transition-colors ${
                tab === t ? "border-foreground text-foreground" : "border-transparent text-muted-foreground"
              }`}
            >
              {t === "overview" ? "Overview" : "Org context"}
            </button>
          ))}
        </div>
      )}

      {tab === "org" && score ? (
        <OrgChart prospect={prospect} />
      ) : (
        <>
          {inProgress ? (
            <ProgressView run={run!} />
          ) : score ? (
            <div className="grid md:grid-cols-12 gap-10">
              <div className="md:col-span-5">
                <BigScore value={score.overall_score} />
                <div className="mt-10 space-y-6 max-w-sm">
                  <ScoreBar
                    label="Authenticity"
                    value={score.authenticity_score}
                    hint="Is this a real, credible person?"
                  />
                  <ScoreBar
                    label="Authority"
                    value={score.authority_score}
                    hint="Are they the right decision-maker?"
                  />
                  <ScoreBar
                    label="Warmth"
                    value={score.warmth_score}
                    hint="Shared connections / context."
                  />
                </div>
              </div>

              <div className="md:col-span-7 space-y-px">
                <WebPresence signals={signals} />

                {/* Phase A6 — rich Tier-1 enrichment surface (mirrors NodeInspector).
                    Sections render only when their backing data is present. */}
                <PersonProfileCard
                  person={{
                    canonical_name: prospect.name,
                    current_title: prospect.role ?? null,
                    current_company_name: prospect.company ?? null,
                    linkedin_url: prospect.linkedin_url ?? null,
                    email: prospect.email ?? null,
                  }}
                  className="border border-border"
                />
                {employment.length > 0 ? (
                  <div className="border border-border p-5">
                    <div className="label-eyebrow mb-3">Career history</div>
                    <CareerTimeline employment={employment} maxRows={8} />
                  </div>
                ) : null}
                {education.length > 0 ? (
                  <div className="border border-border p-5">
                    <div className="label-eyebrow mb-3">Education</div>
                    <EducationTimeline education={education} maxRows={5} />
                  </div>
                ) : null}
                {skills.length > 0 ? (
                  <div className="border border-border p-5">
                    <div className="label-eyebrow mb-3">Top skills</div>
                    <SkillsChipCloud skills={skills} topN={20} />
                  </div>
                ) : null}

                <div className="border border-border">
                  <button
                    onClick={() => setExpanded((v) => !v)}
                    className="w-full flex items-center justify-between p-5 hover:bg-secondary transition-colors"
                  >
                    <div>
                      <div className="label-eyebrow mb-1">Signal breakdown</div>
                      <div className="text-sm">
                        {signals.length} signals · {new Set(signals.map((s) => s.source)).size} sources
                      </div>
                    </div>
                    <div className="text-mono text-xs">{expanded ? "−" : "+"}</div>
                  </button>
                  {expanded && (
                    <div className="border-t border-border">
                      <label className="border-b border-border flex items-center px-4 gap-3 py-2">
                        <span className="text-muted-foreground text-[10px] uppercase tracking-[0.16em]">
                          Search
                        </span>
                        <input
                          type="text"
                          value={signalQuery}
                          onChange={(e) => setSignalQuery(e.target.value)}
                          placeholder="filter by source, type, value, raw…"
                          aria-label="Filter signals"
                          className="flex-1 bg-transparent outline-none text-xs placeholder:text-muted-foreground/50"
                        />
                        {signalQuery && (
                          <span className="text-[10px] text-muted-foreground text-mono">
                            {filteredSignals.length}/{signals.length}
                          </span>
                        )}
                      </label>
                      <table className="w-full text-xs">
                        <thead className="text-muted-foreground">
                          <tr className="border-b border-border">
                            <th className="text-left p-3 font-normal">Source</th>
                            <th className="text-left p-3 font-normal">Signal</th>
                            <th className="text-right p-3 font-normal">Value</th>
                            <th className="text-right p-3 font-normal">Weight</th>
                            <th className="text-right p-3 font-normal">Confidence</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredSignals.map((s) => (
                            <tr key={s._id} className="border-b border-border/60">
                              <td className="p-3 text-mono text-muted-foreground">{s.source}</td>
                              <td className="p-3">{s.signal_type}</td>
                              <td className="p-3 text-right text-mono">{String(s.value)}</td>
                              <td className="p-3 text-right text-mono">{s.weight.toFixed(2)}</td>
                              <td className="p-3 text-right text-mono">
                                {(s.confidence * 100).toFixed(0)}%
                              </td>
                            </tr>
                          ))}
                          {filteredSignals.length === 0 && (
                            <tr>
                              <td
                                colSpan={5}
                                className="p-4 text-center text-muted-foreground text-xs"
                              >
                                No signals match "{signalQuery}".
                              </td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                <div className="border border-border">
                  <button
                    onClick={() => setShowRaw((v) => !v)}
                    className="w-full flex items-center justify-between p-5 hover:bg-secondary transition-colors"
                  >
                    <div>
                      <div className="label-eyebrow mb-1">Raw data</div>
                      <div className="text-sm text-muted-foreground">For debugging</div>
                    </div>
                    <div className="text-mono text-xs">{showRaw ? "−" : "+"}</div>
                  </button>
                  {showRaw && (
                    <pre className="text-mono text-[11px] p-5 border-t border-border overflow-auto max-h-96 text-muted-foreground">
                      {JSON.stringify({ prospect, score, signals }, null, 2)}
                    </pre>
                  )}
                </div>
              </div>
            </div>
          ) : null}

          {!inProgress && !score && (
            <NoScoreState
              runStatus={run?.status ?? null}
              errorLog={run?.error_log ?? null}
              onRetry={() => {
                if (id) void db.runScoring(id);
              }}
            />
          )}
        </>
      )}
    </PageShell>
  );
};

const NoScoreState = ({
  runStatus,
  errorLog,
  onRetry,
}: {
  runStatus: string | null;
  errorLog: string | null;
  onRetry: () => void;
}) => {
  const [retrying, setRetrying] = useState(false);
  const handleRetry = () => {
    setRetrying(true);
    onRetry();
    // Pause the button briefly so repeated clicks don't dogpile the edge function.
    setTimeout(() => setRetrying(false), 2000);
  };

  const failed = runStatus === "error";
  return (
    <div className="border border-border p-6 max-w-2xl">
      <div className="label-eyebrow mb-3">
        {failed ? "Scoring failed" : "No score yet"}
      </div>
      <p className="text-sm text-muted-foreground leading-relaxed mb-4">
        {failed
          ? "The scoring agent returned an error on its last run. You can retry below."
          : runStatus === null
            ? "Scoring runs via the FastAPI worker (server/) backed by Z.AI. If this page stays empty for more than ~90 seconds the worker may not be reachable, or the ZAI_API_KEY env var may be missing on the server. Retry below to kick off another run."
            : `Run status: ${runStatus}. Waiting for the agent to write a score row.`}
      </p>
      {failed && errorLog && (
        <pre className="text-mono text-[11px] p-3 border border-danger/30 bg-danger/5 text-muted-foreground mb-4 overflow-auto max-h-32">
          {errorLog}
        </pre>
      )}
      <button
        onClick={handleRetry}
        disabled={retrying}
        className="border border-border px-4 py-2 text-xs uppercase tracking-[0.16em] hover:bg-secondary transition-colors disabled:opacity-40"
      >
        {retrying ? "Retrying…" : "Re-run scoring"}
      </button>
    </div>
  );
};

const AgentStep = ({ step }: { step: Record<string, unknown> }) => {
  const type = step.type as string;
  if (type === "search") return (
    <div className="px-4 py-2 text-xs flex gap-3">
      <span className="text-mono text-muted-foreground/60 shrink-0">search</span>
      <span className="truncate">{step.query as string}</span>
    </div>
  );
  if (type === "signal") return (
    <div className="px-4 py-2 text-xs flex gap-3">
      <span className="text-mono text-muted-foreground/60 shrink-0">signal</span>
      <span>{step.signal_type as string} = {String(step.value)}</span>
      <span className="text-muted-foreground/40 ml-auto shrink-0">{step.source as string}</span>
    </div>
  );
  if (type === "finalized") return (
    <div className="px-4 py-2 text-xs flex gap-3" style={{ color: "hsl(var(--success))" }}>
      <span className="text-mono shrink-0">identified</span>
      <span className="font-medium">{step.name as string}</span>
      <span className="opacity-60 ml-1">({Math.round(((step.confidence as number) ?? 0) * 100)}% confidence)</span>
    </div>
  );
  return null;
};

const ProgressView = ({ run }: { run: ScoringRun }) => {
  const steps = (run.agent_steps ?? []) as Array<Record<string, unknown>>;
  return (
    <div className="max-w-2xl">
      <div className="label-eyebrow mb-3">Agent working…</div>
      <div className="text-3xl font-light tracking-tight mb-8">
        {run.current_source ?? "starting"}
      </div>
      <div className="space-y-2 mb-8">
        {ALL_SOURCES.map((s) => {
          const ok = run.sources_succeeded.includes(s);
          const active = run.current_source === s;
          return (
            <div
              key={s}
              className="flex items-center justify-between border-b border-border py-2 text-xs"
            >
              <span className="text-mono text-muted-foreground">{s}</span>
              <span
                className={`text-mono ${
                  ok ? "" : active ? "" : "text-muted-foreground/50"
                }`}
                style={{ color: ok ? "hsl(var(--success))" : active ? "hsl(var(--accent))" : undefined }}
              >
                {ok ? "OK" : active ? "…" : "queued"}
              </span>
            </div>
          );
        })}
      </div>

      {steps.length > 0 && (
        <div className="border border-border">
          <div className="px-4 py-3 border-b border-border">
            <div className="label-eyebrow">Agent reasoning</div>
          </div>
          <div className="divide-y divide-border/60 max-h-64 overflow-y-auto">
            {steps.map((step, i) => (
              <AgentStep key={i} step={step} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

const API_BASE: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/+$/, "") ||
  "http://localhost:8000";

const MIN_PEERS_FOR_CHART = 3;

interface FastApiProspect {
  _id: string;
  name: string;
  role: string;
  company: string;
  industry?: string;
  linkedin_url?: string;
}
interface FastApiProspectsResp {
  prospects: FastApiProspect[];
}

// Reject placeholder / sentinel names ("Unknown", empty, whitespace-only) so
// they never end up as peers in the org graph.
function isMeaningfulName(name: string | null | undefined): boolean {
  if (!name) return false;
  const trimmed = name.trim();
  if (!trimmed) return false;
  if (/^unknown$/i.test(trimmed)) return false;
  return true;
}

function normalizeCompany(s: string | null | undefined): string {
  return (s ?? "")
    .toLowerCase()
    .replace(
      /\b(corp\.?|corporation|inc\.?|incorporated|limited|ltd\.?|llc|plc|technologies|technology|semiconductor|semiconductors|systems?)\b/g,
      "",
    )
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

async function fetchFastApiPeers(
  company: string,
  industry: string | undefined,
  excludeId: string | undefined,
): Promise<OrgPerson[]> {
  const target = normalizeCompany(company);
  if (!target) return [];

  // Our FastAPI `/convex/prospects` filters by industry (ILIKE) — we fetch the
  // industry's full list once and client-side match on company name, with
  // normalization to handle "Intel" vs "Intel Corporation" vs "Intel Corp".
  const industryCandidates = [industry, "semiconductor", "defense"].filter(
    (v, i, arr): v is string => !!v && arr.indexOf(v) === i,
  );
  const seen = new Map<string, OrgPerson>();
  for (const ind of industryCandidates) {
    try {
      const url = `${API_BASE}/convex/prospects?industry=${encodeURIComponent(
        ind,
      )}&limit=500`;
      // `getCredenceHeaders()` attaches `X-Credence-Demo: true` in demo mode
      // (Wave 6 M5) and will attach `Authorization: Bearer <jwt>` once M3
      // wires authenticated live mode.
      const resp = await fetch(url, { headers: getCredenceHeaders() });
      if (!resp.ok) continue;
      const body = (await resp.json()) as FastApiProspectsResp;
      for (const p of body.prospects ?? []) {
        if (!isMeaningfulName(p.name) || !p.role || !p.company) continue;
        if (excludeId && p._id === excludeId) continue;
        const pNorm = normalizeCompany(p.company);
        if (!pNorm) continue;
        const match =
          pNorm === target || pNorm.includes(target) || target.includes(pNorm);
        if (!match) continue;
        if (!seen.has(p._id)) {
          seen.set(p._id, {
            id: p._id,
            name: p.name,
            role: p.role,
            company: p.company,
            industry: p.industry,
            linkedin_url: p.linkedin_url,
            source: "fastapi",
          });
        }
      }
      if (seen.size >= 10) break;
    } catch (err) {
      console.error("[OrgChart] FastAPI fetch failed:", err);
    }
  }
  return Array.from(seen.values());
}

function useOrgPeers(
  company: string | undefined,
  industry: string | undefined,
  excludeId: string | undefined,
): {
  peers: OrgPerson[];
  loading: boolean;
  source: "supabase" | "fastapi" | "mixed" | null;
} {
  const [peers, setPeers] = useState<OrgPerson[]>([]);
  const [source, setSource] = useState<"supabase" | "fastapi" | "mixed" | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!company) {
      setLoading(false);
      setPeers([]);
      return;
    }
    let cancelled = false;
    setLoading(true);

    const run = async () => {
      // 1. Supabase first — cheap, already-ETL'd prospects at that company.
      let supaPeers: OrgPerson[] = [];
      if (supabase) {
        const { data, error } = await supabase
          .from("prospects")
          .select("id,name,role,company")
          .ilike("company", company)
          .neq("id", excludeId ?? "")
          .limit(40);
        if (error) {
          console.error("[OrgChart] supabase peer fetch failed:", error);
        } else {
          supaPeers = (data ?? [])
            .filter(
              (row): row is OrgPerson => isMeaningfulName(row.name) && !!row.role,
            )
            .map((row) => ({
              id: row.id,
              name: row.name,
              role: row.role,
              company: row.company,
              source: "supabase" as const,
            }));
        }
      }
      if (cancelled) return;

      // 2. If Supabase is sparse, augment from FastAPI (covers all 2059
      //    lead_scoring.people rows including un-ETL'd Intel/Lockheed/etc.).
      let merged = supaPeers;
      let src: "supabase" | "fastapi" | "mixed" = "supabase";
      if (supaPeers.length < MIN_PEERS_FOR_CHART) {
        const fastApiPeers = await fetchFastApiPeers(company, industry, excludeId);
        if (cancelled) return;

        const keyOf = (p: OrgPerson) =>
          `${p.name.toLowerCase().trim()}|${normalizeCompany(p.company)}`;
        const seen = new Set(supaPeers.map(keyOf));
        const extras = fastApiPeers.filter((p) => !seen.has(keyOf(p)));
        merged = [...supaPeers, ...extras];
        src = supaPeers.length > 0 ? "mixed" : "fastapi";
      }

      if (cancelled) return;
      setPeers(merged);
      setSource(merged.length > 0 ? src : null);
      setLoading(false);
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [company, industry, excludeId]);

  return { peers, loading, source };
}

// ─── v3 Org reporting edges (Task 3-A/3-B/3-C) ─────────────────────────────
//
// Reads from `org_reporting_edges` with stub-aware joins. Returns the raw
// edge rows + a person-id → person record map so the chart layer can render
// both real persons and `is_unresolved_target` stubs without re-querying.
//
// Resolution path (prospect → person → company → org_reporting_edges):
//   1. Find the `persons` row by linkedin_url (preferred) then canonical_name.
//   2. Read that person's current company from employment_periods (is_current).
//   3. Pull every is_current employment_period for the same company → person ids.
//   4. Query org_reporting_edges where BOTH endpoints sit in that person id set,
//      is_current=TRUE, ordered by path_confidence desc nulls last.
//   5. Re-join persons (manager_id + report_id) to get name/title/stub flag.
//
// Returns { edges: [], persons: Map<id, OrgPersonRecord>, ready: bool }.
// `ready=false` means we couldn't resolve the prospect → person yet (or we
// did and the company has 0 v3 edges — caller falls back to v2).

interface OrgPersonRecord {
  id: string;
  canonical_name: string;
  current_title: string | null;
  is_unresolved_target: boolean;
}

interface OrgReportingEdgeRow {
  id: string;
  manager_id: string;
  report_id: string;
  confidence: number | null;
  path_confidence: number | null;
  inference_method: string;
  valid_from: string | null;
  is_current: boolean;
}

interface OrgV3Result {
  edges: OrgReportingEdgeRow[];
  persons: Map<string, OrgPersonRecord>;
  companyName: string | null;
}

async function fetchOrgV3(
  prospect: OrgPerson,
): Promise<OrgV3Result | null> {
  if (!supabase) return null;
  // The Database type generated for the supabase client only knows about v2
  // tables (prospects/signals/scores). v3 tables (persons, employment_periods,
  // org_reporting_edges) exist in the database but aren't in the generated
  // types yet, so we go through an untyped client for these reads. Once
  // database.types.ts is regenerated this can be removed.
  const sb = supabase as unknown as {
    from: (table: string) => {
      select: (cols: string) => {
        eq: (col: string, v: unknown) => {
          limit: (n: number) => Promise<{ data: unknown; error: unknown }>;
          maybeSingle?: () => Promise<{ data: unknown; error: unknown }>;
          eq: (col: string, v: unknown) => {
            order: (col: string, opts?: unknown) => {
              limit: (n: number) => Promise<{ data: unknown; error: unknown }>;
            };
          };
          in?: (col: string, v: unknown[]) => Promise<{ data: unknown; error: unknown }>;
        };
        in: (col: string, v: unknown[]) => {
          eq: (col: string, v: unknown) => {
            order: (col: string, opts?: unknown) => {
              limit: (n: number) => Promise<{ data: unknown; error: unknown }>;
            };
          };
        };
        ilike: (col: string, v: string) => {
          limit: (n: number) => Promise<{ data: unknown; error: unknown }>;
        };
      };
    };
  };

  try {
    // 1. Resolve prospect → person.
    let personRow: { id: string; current_company_id: string | null } | null = null;
    if (prospect.linkedin_url) {
      const r = (await sb
        .from("persons")
        .select("id, current_company_id")
        .eq("linkedin_url", prospect.linkedin_url)
        .limit(1)) as { data: unknown; error: unknown };
      if (Array.isArray(r.data) && r.data.length > 0) {
        personRow = r.data[0] as typeof personRow;
      }
    }
    if (!personRow) {
      const r = (await sb
        .from("persons")
        .select("id, current_company_id")
        .ilike("canonical_name", prospect.name)
        .limit(1)) as { data: unknown; error: unknown };
      if (Array.isArray(r.data) && r.data.length > 0) {
        personRow = r.data[0] as typeof personRow;
      }
    }
    if (!personRow) return null;

    // 2. Resolve company id (prefer employment_periods.is_current).
    let companyId: string | null = personRow.current_company_id;
    if (!companyId) {
      const r = (await sb
        .from("employment_periods")
        .select("company_id")
        .eq("person_id", personRow.id)
        .eq("is_current", true)
        .order("start_year", { ascending: false })
        .limit(1)) as { data: unknown; error: unknown };
      if (Array.isArray(r.data) && r.data.length > 0) {
        companyId = (r.data[0] as { company_id: string }).company_id ?? null;
      }
    }
    if (!companyId) return null;

    // 3. All current employees of that company.
    const empResp = (await sb
      .from("employment_periods")
      .select("person_id")
      .eq("company_id", companyId)
      .eq("is_current", true)
      .order("seniority_score", { ascending: false })
      .limit(500)) as { data: unknown; error: unknown };
    const personIds = Array.isArray(empResp.data)
      ? Array.from(
          new Set(
            (empResp.data as { person_id: string }[])
              .map((r) => r.person_id)
              .filter((v): v is string => typeof v === "string"),
          ),
        )
      : [];
    if (personIds.length === 0) {
      return { edges: [], persons: new Map(), companyName: null };
    }

    // 4. Query org_reporting_edges where both endpoints sit in personIds.
    //    org_reporting_edges has no company_id; we filter in JS after fetching
    //    edges that touch the persons we care about.
    const edgesResp = (await sb
      .from("org_reporting_edges")
      .select(
        "id, manager_id, report_id, confidence, path_confidence, inference_method, valid_from, is_current",
      )
      .in("manager_id", personIds)
      .eq("is_current", true)
      .order("path_confidence", { ascending: false, nullsFirst: false } as unknown as undefined)
      .limit(500)) as { data: unknown; error: unknown };

    const personIdSet = new Set(personIds);
    const rawEdges: OrgReportingEdgeRow[] = Array.isArray(edgesResp.data)
      ? (edgesResp.data as OrgReportingEdgeRow[]).filter(
          (e) =>
            typeof e.report_id === "string" &&
            typeof e.manager_id === "string" &&
            personIdSet.has(e.report_id) &&
            personIdSet.has(e.manager_id),
        )
      : [];

    if (rawEdges.length === 0) {
      return { edges: [], persons: new Map(), companyName: null };
    }

    // 5. Re-join persons for endpoint metadata.
    const endpointIds = Array.from(
      new Set(rawEdges.flatMap((e) => [e.manager_id, e.report_id])),
    );
    const personsResp = (await (
      sb
        .from("persons")
        .select(
          "id, canonical_name, current_title, is_unresolved_target",
        ) as unknown as {
        in: (col: string, v: unknown[]) => Promise<{ data: unknown; error: unknown }>;
      }
    ).in("id", endpointIds)) as { data: unknown; error: unknown };
    const personsMap = new Map<string, OrgPersonRecord>();
    if (Array.isArray(personsResp.data)) {
      for (const row of personsResp.data as Array<{
        id: string;
        canonical_name: string;
        current_title: string | null;
        is_unresolved_target: boolean | null;
      }>) {
        personsMap.set(row.id, {
          id: row.id,
          canonical_name: row.canonical_name,
          current_title: row.current_title ?? null,
          is_unresolved_target: row.is_unresolved_target === true,
        });
      }
    }

    return { edges: rawEdges, persons: personsMap, companyName: null };
  } catch (err) {
    console.error("[OrgChart] v3 fetch failed:", err);
    return null;
  }
}

function useOrgV3(prospect: OrgPerson): {
  data: OrgV3Result | null;
  loading: boolean;
} {
  const [data, setData] = useState<OrgV3Result | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void fetchOrgV3(prospect).then((res) => {
      if (cancelled) return;
      setData(res);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [prospect.id, prospect.linkedin_url, prospect.name]);

  return { data, loading };
}

// Layered tree layout — roots (no manager among current edges) at the top,
// reports flow downward by BFS depth. Pure layout; no side effects on the
// graph data model.
function layoutOrgTree(
  edges: OrgReportingEdgeRow[],
  persons: Map<string, OrgPersonRecord>,
): Map<string, { x: number; y: number; depth: number }> {
  const layout = new Map<string, { x: number; y: number; depth: number }>();
  const childrenOf = new Map<string, string[]>();
  const hasManager = new Set<string>();
  const allIds = new Set<string>();
  for (const e of edges) {
    allIds.add(e.manager_id);
    allIds.add(e.report_id);
    hasManager.add(e.report_id);
    const arr = childrenOf.get(e.manager_id) ?? [];
    arr.push(e.report_id);
    childrenOf.set(e.manager_id, arr);
  }
  const roots = Array.from(allIds).filter((id) => !hasManager.has(id));
  if (roots.length === 0 && allIds.size > 0) {
    // Cycle-only edge set — pick the first id as a synthetic root.
    roots.push(Array.from(allIds)[0]);
  }
  // BFS to assign depth.
  const depth = new Map<string, number>();
  const queue: string[] = [];
  for (const r of roots) {
    depth.set(r, 0);
    queue.push(r);
  }
  while (queue.length > 0) {
    const id = queue.shift() as string;
    const d = depth.get(id) ?? 0;
    for (const child of childrenOf.get(id) ?? []) {
      if (!depth.has(child)) {
        depth.set(child, d + 1);
        queue.push(child);
      }
    }
  }
  // Ensure every node has a depth (orphans land on row 0).
  for (const id of allIds) {
    if (!depth.has(id)) depth.set(id, 0);
  }
  // Place by depth row, x = horizontal slot.
  const byDepth = new Map<number, string[]>();
  for (const [id, d] of depth) {
    const arr = byDepth.get(d) ?? [];
    arr.push(id);
    byDepth.set(d, arr);
  }
  // Stable order within each row by canonical_name for determinism.
  for (const arr of byDepth.values()) {
    arr.sort((a, b) => {
      const an = persons.get(a)?.canonical_name ?? a;
      const bn = persons.get(b)?.canonical_name ?? b;
      return an.localeCompare(bn);
    });
  }
  const ROW_H = 150;
  const COL_W = 220;
  for (const [d, ids] of byDepth) {
    const mid = (ids.length - 1) / 2;
    ids.forEach((id, i) => {
      layout.set(id, { x: (i - mid) * COL_W, y: d * ROW_H, depth: d });
    });
  }
  return layout;
}

// Confidence band → stroke color (3-tier traffic light).
function confidenceColor(conf: number): string {
  if (conf >= 0.8) return "#10B981";
  if (conf >= 0.5) return "#F59E0B";
  return "#EF4444";
}

function relativeTime(iso: string | null): string {
  if (!iso) return "unknown";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "unknown";
  const days = Math.max(1, Math.round((Date.now() - t) / 86_400_000));
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${Math.round(days / 365)}y ago`;
}

// ── Stub node — Decision 4: unknown nodes are rendered, not omitted. ────────
const stubNodeStyle: React.CSSProperties = {
  border: "2px dashed #9CA3AF",
  background: "#F8F8F8",
  fontStyle: "italic",
  padding: "8px 12px",
  borderRadius: 4,
  position: "relative",
  minWidth: 160,
  maxWidth: 220,
  fontSize: 11,
  fontFamily: "Inter",
  color: "#374151",
};

const StubNode = ({ data }: NodeProps) => {
  const d = data as { label: string };
  return (
    <div style={stubNodeStyle}>
      <span
        style={{
          position: "absolute",
          top: -8,
          right: -8,
          background: "#9CA3AF",
          color: "white",
          borderRadius: "50%",
          width: 18,
          height: 18,
          fontSize: 12,
          textAlign: "center",
          lineHeight: "18px",
        }}
        aria-label="Unresolved person"
      >
        ?
      </span>
      <div>{d.label}</div>
      <div style={{ fontSize: 10, color: "#6B7280", marginTop: 2, fontStyle: "normal" }}>
        Role inferred · Person not yet identified
      </div>
    </div>
  );
};

const PersonNode = ({ data }: NodeProps) => {
  const d = data as { label: string; isCenter?: boolean };
  return (
    <div
      style={nodeStyle(d.isCenter ? "center" : "peer")}
      title={d.label}
    >
      {d.label}
    </div>
  );
};

const orgNodeTypes: NodeTypes = {
  stubNode: StubNode,
  personNode: PersonNode,
};

const OrgChart = ({ prospect }: { prospect: OrgPerson & { industry?: string } }) => {
  const navigate = useNavigate();

  // 1. Try v3 first — read from org_reporting_edges.
  const { data: v3, loading: v3Loading } = useOrgV3(prospect);

  // 2. v2 fallback path — used when org_reporting_edges has no edges for this
  //    company. Will be deleted post-Phase-A.7 live run.
  const { peers, loading: v2Loading, source } = useOrgPeers(
    prospect.company,
    prospect.industry,
    prospect.id,
  );

  // Confidence-threshold filter (Task 3-B). Hidden but not removed from state.
  const [minConfidence, setMinConfidence] = useState<number>(0.45);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);
  const [selectedStubId, setSelectedStubId] = useState<string | null>(null);

  const hasV3 = !!v3 && v3.edges.length > 0;
  const loading = v3Loading || (!hasV3 && v2Loading);

  // Build the v3 ReactFlow nodes/edges if we have v3 data.
  const v3Graph = useMemo(() => {
    if (!v3 || v3.edges.length === 0) return null;
    const layout = layoutOrgTree(v3.edges, v3.persons);
    const nodes: Node[] = [];
    for (const [id, pos] of layout) {
      const p = v3.persons.get(id);
      const isStub = p?.is_unresolved_target === true;
      const label = p
        ? `${p.canonical_name}${p.current_title ? `\n${p.current_title}` : ""}`
        : id;
      nodes.push({
        id,
        position: { x: pos.x, y: pos.y },
        type: isStub ? "stubNode" : "personNode",
        data: {
          label,
          person: p,
          is_unresolved_target: isStub,
          canonical_name: p?.canonical_name ?? "",
          current_title: p?.current_title ?? null,
        },
      });
    }
    const edges: Edge[] = v3.edges.map((e) => {
      const conf = e.path_confidence ?? e.confidence ?? 0.5;
      const opacity = Math.max(0.3, conf);
      const strokeWidth = 1 + conf * 2.5;
      const color = confidenceColor(conf);
      const hidden = conf < minConfidence;
      return {
        id: e.id,
        source: e.manager_id,
        target: e.report_id,
        data: {
          confidence: e.confidence,
          path_confidence: e.path_confidence,
          inference_method: e.inference_method,
          valid_from: e.valid_from,
        },
        style: hidden
          ? { opacity: 0, pointerEvents: "none" as const }
          : { stroke: color, strokeWidth, opacity },
        // title attribute on edge path for native tooltip fallback —
        // react-tooltip is not in deps; this still gives the operator
        // source/confidence/recency info on hover.
        label:
          hoveredEdgeId === e.id
            ? `Source: ${e.inference_method} · Confidence: ${Math.round(conf * 100)}% · Last updated: ${relativeTime(e.valid_from)}`
            : undefined,
        labelStyle: { fontSize: 10, fill: "hsl(var(--muted-foreground))" },
      };
    });
    return { nodes, edges };
  }, [v3, minConfidence, hoveredEdgeId]);

  // Click a node → navigate to that prospect (real persons only).
  // Stub nodes open the StubInspector panel instead.
  const [importing, setImporting] = useState<string | null>(null);
  const onNodeClick = async (_e: React.MouseEvent, node: Node) => {
    if (hasV3) {
      const data = node.data as {
        is_unresolved_target?: boolean;
        canonical_name?: string;
        current_title?: string | null;
      };
      if (data.is_unresolved_target) {
        setSelectedStubId(node.id);
        return;
      }
      // Real person — navigate via id.
      navigate(`/prospect/${node.id}`);
      return;
    }
    // v2 fallback path (legacy peer nav + import).
    const data = node.data as { person?: OrgPerson; clickable?: boolean };
    const person = data.person;
    if (!data.clickable || !person) return;
    if (person.source === "supabase") {
      navigate(`/prospect/${person.id}`);
      return;
    }
    if (importing) return;
    setImporting(person.id);
    try {
      const newId = await db.createProspect({
        name: person.name,
        company: person.company ?? "",
        role: person.role,
        industry: person.industry ?? prospect.industry ?? "Semiconductors",
        linkedin_url: person.linkedin_url,
      });
      void db.runScoring(newId);
      navigate(`/prospect/${newId}`);
    } catch (err) {
      console.error("[OrgChart] failed to import fastapi peer:", err);
      setImporting(null);
    }
  };

  if (loading) {
    return (
      <div className="border border-border h-[520px] flex items-center justify-center text-sm text-muted-foreground">
        Loading org context…
      </div>
    );
  }

  if (hasV3 && v3Graph) {
    const selectedStub = selectedStubId
      ? v3?.persons.get(selectedStubId)
      : null;
    return (
      <div className="space-y-3">
        <div className="border border-border relative" style={{ height: 520 }}>
          <ReactFlow
            nodes={v3Graph.nodes}
            edges={v3Graph.edges}
            nodeTypes={orgNodeTypes}
            fitView
            proOptions={{ hideAttribution: true }}
            onNodeClick={onNodeClick}
            onEdgeMouseEnter={(_e, edge) => setHoveredEdgeId(edge.id)}
            onEdgeMouseLeave={() => setHoveredEdgeId(null)}
            nodesDraggable={false}
            nodesConnectable={false}
          >
            <Background color="hsl(var(--border))" gap={24} />
            <Controls className="!bg-secondary !border-border" />
          </ReactFlow>
          <div className="absolute top-2 right-2 text-[10px] uppercase tracking-[0.16em] text-muted-foreground/60 bg-background/80 border border-border px-2 py-1">
            source: org_reporting_edges
          </div>
        </div>
        <div className="flex items-center gap-3 px-1">
          <input
            type="range"
            min="0.40"
            max="0.99"
            step="0.01"
            value={minConfidence}
            onChange={(e) => setMinConfidence(parseFloat(e.target.value))}
            className="flex-1 max-w-xs"
            aria-label="Minimum edge confidence"
          />
          <span className="text-[11px] text-muted-foreground text-mono">
            Min confidence: {Math.round(minConfidence * 100)}%
          </span>
          <span className="text-[11px] text-muted-foreground">
            · {v3Graph.edges.filter((e) => e.style && (e.style as { opacity?: number }).opacity !== 0).length}/{v3Graph.edges.length} edges visible
          </span>
        </div>
        {selectedStub && (
          <StubInspector
            canonicalName={selectedStub.canonical_name}
            currentTitle={selectedStub.current_title}
            inferenceMethod={
              v3?.edges.find(
                (e) =>
                  e.manager_id === selectedStubId ||
                  e.report_id === selectedStubId,
              )?.inference_method ?? "inferred"
            }
            companyName={prospect.company ?? ""}
            onClose={() => setSelectedStubId(null)}
          />
        )}
      </div>
    );
  }

  // ─── v2 fallback path (used when org_reporting_edges has no edges) ───────
  if (peers.length === 0) {
    return (
      <div className="border border-border h-[520px] flex items-center justify-center text-sm text-muted-foreground text-center px-8">
        No co-workers found at {prospect.company || "this company"} yet. Run the
        LinkedIn employee scrape for this company to populate the org graph.
      </div>
    );
  }
  const { nodes, edges } = buildOrgFromData(prospect, peers);
  return (
    <div className="border border-border relative" style={{ height: 520 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        proOptions={{ hideAttribution: true }}
        onNodeClick={onNodeClick}
        nodesDraggable={false}
        nodesConnectable={false}
      >
        <Background color="hsl(var(--border))" gap={24} />
        <Controls className="!bg-secondary !border-border" />
      </ReactFlow>
      {source && source !== "supabase" && (
        <div className="absolute top-2 right-2 text-[10px] uppercase tracking-[0.16em] text-muted-foreground/60 bg-background/80 border border-border px-2 py-1">
          source: {source}
        </div>
      )}
      {importing && (
        <div className="absolute bottom-2 left-2 text-[10px] uppercase tracking-[0.16em] text-muted-foreground bg-background/90 border border-border px-2 py-1">
          importing peer + triggering score…
        </div>
      )}
      <div className="absolute bottom-2 right-2 text-[10px] text-muted-foreground/60 bg-background/80 border border-border px-2 py-1">
        click a peer to open their profile
      </div>
    </div>
  );
};

function buildOrgFromData(
  prospect: OrgPerson,
  peers: OrgPerson[],
): { nodes: Node[]; edges: Edge[] } {
  const selfRank = seniorityRank(prospect.role);
  const ranked = peers
    .map((p) => ({ person: p, rank: seniorityRank(p.role) }))
    // Bias toward title diversity: stable-sort by |rank - selfRank| ASC so
    // nearest-seniority peers come first, then managers/reports.
    .sort((a, b) => Math.abs(a.rank - selfRank) - Math.abs(b.rank - selfRank));

  const managers = ranked.filter((r) => r.rank > selfRank + 8);
  const reportsPool = ranked.filter((r) => r.rank < selfRank - 8);
  const peerPool = ranked.filter((r) => Math.abs(r.rank - selfRank) <= 8);

  // Pick up to 1 manager (highest-ranked), up to 4 peers, up to 3 reports.
  const manager = managers.sort((a, b) => b.rank - a.rank)[0] ?? null;
  const chosenPeers = peerPool.slice(0, 4);
  const chosenReports = reportsPool.sort((a, b) => b.rank - a.rank).slice(0, 3);

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Center (the target prospect) — not clickable; user is already on this page.
  nodes.push({
    id: "center",
    position: { x: 0, y: 0 },
    data: { label: orgLabel(prospect.name, prospect.role), clickable: false },
    style: nodeStyle("center"),
  });

  // Manager above — typically a VP/C-level at the same company.
  if (manager) {
    nodes.push({
      id: `m-${manager.person.id}`,
      position: { x: 0, y: -170 },
      data: {
        label: orgLabel(manager.person.name, manager.person.role),
        person: manager.person,
        clickable: true,
      },
      style: nodeStyle("manager"),
    });
    edges.push({
      id: `em-${manager.person.id}`,
      source: `m-${manager.person.id}`,
      target: "center",
      style: { stroke: "hsl(var(--border))" },
    });
  }

  // Peers to the sides (split left/right).
  const peerSpread = 280;
  chosenPeers.forEach((pr, i) => {
    const half = Math.ceil(chosenPeers.length / 2);
    const side = i < half ? -1 : 1;
    const indexInSide = i < half ? i : i - half;
    const countInSide = i < half ? half : chosenPeers.length - half;
    const yOffset =
      countInSide === 1 ? 0 : (indexInSide - (countInSide - 1) / 2) * 90;
    nodes.push({
      id: `p-${pr.person.id}`,
      position: { x: side * peerSpread, y: yOffset },
      data: {
        label: orgLabel(pr.person.name, pr.person.role),
        person: pr.person,
        clickable: true,
      },
      style: nodeStyle("peer"),
    });
    edges.push({
      id: `ep-${pr.person.id}`,
      source: "center",
      target: `p-${pr.person.id}`,
      style: { stroke: "hsl(var(--border))", strokeDasharray: "4 4" },
    });
  });

  // Reports below.
  const reportSpread = 180;
  chosenReports.forEach((rp, i) => {
    const mid = (chosenReports.length - 1) / 2;
    nodes.push({
      id: `r-${rp.person.id}`,
      position: { x: (i - mid) * reportSpread, y: 170 },
      data: {
        label: orgLabel(rp.person.name, rp.person.role),
        person: rp.person,
        clickable: true,
      },
      style: nodeStyle("report"),
    });
    edges.push({
      id: `er-${rp.person.id}`,
      source: "center",
      target: `r-${rp.person.id}`,
      style: { stroke: "hsl(var(--border))" },
    });
  });

  return { nodes, edges };
}

function orgLabel(name: string, role: string): string {
  // Truncate long roles so nodes stay readable.
  const trimmedRole = role.length > 48 ? role.slice(0, 45) + "…" : role;
  return `${name}\n${trimmedRole}`;
}

type NodeKind = "center" | "manager" | "peer" | "report";
const nodeStyle = (kind: NodeKind): React.CSSProperties => {
  const isCenter = kind === "center";
  return {
    background: isCenter ? "hsl(var(--foreground))" : "hsl(var(--card))",
    color: isCenter ? "hsl(var(--background))" : "hsl(var(--foreground))",
    border: `1px solid ${
      kind === "manager" ? "hsl(var(--accent))" : "hsl(var(--border))"
    }`,
    borderRadius: 2,
    fontSize: 11,
    padding: 10,
    fontFamily: "Inter",
    whiteSpace: "pre-line",
    textAlign: "center",
    minWidth: 140,
    maxWidth: 220,
    cursor: isCenter ? "default" : "pointer",
  };
};

export default ProspectDetail;
