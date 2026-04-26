import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import {
  db,
  useProspect,
  useSignalsFor,
  useLatestScore,
  useLatestRun,
} from "@/lib/db";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import { BigScore, ScoreBar, scoreColor } from "@/components/ScoreBar";
import { ENABLE_ORG_CHART, supabase } from "@/lib/supabase";
import { WebPresence } from "@/components/WebPresence";
import ReactFlow, { Background, Controls, type Node, type Edge } from "reactflow";
import "reactflow/dist/style.css";

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
        <div className="text-muted-foreground text-sm">Prospect not found.</div>
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
            ? "Scoring runs via the Claude agent in a Supabase Edge Function. If this page stays empty for more than ~90 seconds, the function may not be deployed or the ANTHROPIC_API_KEY env var may be missing. Retry below to kick off another run."
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

const ProgressView = ({ run }: { run: any }) => {
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
      const resp = await fetch(url);
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

const OrgChart = ({ prospect }: { prospect: OrgPerson & { industry?: string } }) => {
  const navigate = useNavigate();
  const { peers, loading, source } = useOrgPeers(
    prospect.company,
    prospect.industry,
    prospect.id,
  );
  const { nodes, edges } = useMemo(
    () => buildOrgFromData(prospect, peers),
    [prospect, peers],
  );

  // Click a peer node → navigate to their /prospect/:id page. Supabase-sourced
  // peers route directly by id. FastAPI-sourced peers don't exist in
  // `public.prospects` yet; we upsert them first (so `/prospect/:id` resolves)
  // and kick off scoring, then route.
  const [importing, setImporting] = useState<string | null>(null);
  const onNodeClick = async (_e: React.MouseEvent, node: Node) => {
    const data = node.data as { person?: OrgPerson; clickable?: boolean };
    const person = data.person;
    if (!data.clickable || !person) return;
    if (person.source === "supabase") {
      navigate(`/prospect/${person.id}`);
      return;
    }
    // FastAPI peer — import into Supabase, then navigate.
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
  if (peers.length === 0) {
    return (
      <div className="border border-border h-[520px] flex items-center justify-center text-sm text-muted-foreground text-center px-8">
        No co-workers found at {prospect.company || "this company"} yet. Run the
        LinkedIn employee scrape for this company to populate the org graph.
      </div>
    );
  }
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
