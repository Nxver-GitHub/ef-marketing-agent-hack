import { useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import {
  useProspect,
  useSignalsFor,
  useLatestScore,
  useLatestRun,
} from "@/lib/db";
import { BigScore, ScoreBar, scoreColor } from "@/components/ScoreBar";
import { ENABLE_ORG_CHART } from "@/lib/supabase";
import ReactFlow, { Background, Controls, type Node, type Edge } from "reactflow";
import "reactflow/dist/style.css";

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
  const signals = useSignalsFor(id);
  const score = useLatestScore(id);
  const run = useLatestRun(id);
  const [tab, setTab] = useState<"overview" | "org">("overview");
  const [showRaw, setShowRaw] = useState(false);
  const [expanded, setExpanded] = useState(false);

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
                <div className="border border-warning/40 bg-warning/5 p-5">
                  <div
                    className="label-eyebrow mb-3"
                    style={{ color: "hsl(var(--warning))" }}
                  >
                    Falsification notes
                  </div>
                  <ul className="space-y-2 text-sm">
                    {score.falsification_notes.map((n, i) => (
                      <li key={i} className="flex gap-3">
                        <span className="text-mono text-xs text-muted-foreground mt-0.5">
                          {String(i + 1).padStart(2, "0")}
                        </span>
                        <span>{n}</span>
                      </li>
                    ))}
                  </ul>
                </div>

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
                          {signals.map((s) => (
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
            <div className="text-sm text-muted-foreground">No score yet.</div>
          )}
        </>
      )}
    </PageShell>
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

const OrgChart = ({ prospect }: { prospect: any }) => {
  // TODO: replace mock org graph with real data from fetchOrgChart()
  const { nodes, edges } = useMemo(() => buildMockOrg(prospect), [prospect]);
  return (
    <div className="border border-border" style={{ height: 520 }}>
      <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
        <Background color="hsl(var(--border))" gap={24} />
        <Controls className="!bg-secondary !border-border" />
      </ReactFlow>
    </div>
  );
};

function buildMockOrg(p: any): { nodes: Node[]; edges: Edge[] } {
  const center: Node = {
    id: "center",
    position: { x: 0, y: 0 },
    data: { label: `${p.name} · ${p.role}` },
    style: nodeStyle(true),
  };
  const peers = [
    "VP Engineering",
    "Director Ops",
    "Principal Architect",
    "Sr Manager R&D",
    "Head of Strategy",
    "VP Finance",
    "EVP Product",
  ].map((title, i, arr) => {
    const a = (i / arr.length) * Math.PI * 2;
    return {
      id: `p${i}`,
      position: { x: Math.cos(a) * 240, y: Math.sin(a) * 180 },
      data: { label: `${["A. Tan","M. Ortiz","K. Singh","D. Park","J. Liu","R. Schmidt","Y. Brown"][i]} · ${title}` },
      style: nodeStyle(false),
    } as Node;
  });
  const edges: Edge[] = peers.map((n) => ({
    id: `e-${n.id}`,
    source: "center",
    target: n.id,
    style: { stroke: "hsl(var(--border))" },
  }));
  return { nodes: [center, ...peers], edges };
}

const nodeStyle = (center: boolean): React.CSSProperties => ({
  background: center ? "hsl(var(--foreground))" : "hsl(var(--card))",
  color: center ? "hsl(var(--background))" : "hsl(var(--foreground))",
  border: "1px solid hsl(var(--border))",
  borderRadius: 2,
  fontSize: 11,
  padding: 8,
  fontFamily: "Inter",
});

export default ProspectDetail;
