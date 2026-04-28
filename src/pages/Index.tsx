import { Link } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { HeroMark } from "@/components/HeroMark";
import { useProspects, useScoresFor } from "@/lib/db";
import { useMemo } from "react";
import { scoreColor } from "@/components/ScoreBar";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

const Index = () => {
  useDocumentTitle(null);
  const prospects = useProspects();
  const ids = useMemo(() => prospects.map((p) => p._id), [prospects]);
  const scores = useScoresFor(ids);
  const scored = useMemo(() => prospects.filter((p) => scores[p._id]), [prospects, scores]);
  const avgScore = scored.length
    ? scored.reduce((s, p) => s + (scores[p._id]?.overall_score ?? 0), 0) / scored.length
    : null;

  // Derived headline numbers — used as the small footnote under each card.
  const companyCount = useMemo(() => {
    const set = new Set<string>();
    for (const p of prospects) if (p.company) set.add(p.company);
    return set.size;
  }, [prospects]);

  return (
    <PageShell rightSlot={import.meta.env.DEV ? <div className="text-mono text-[10px] text-muted-foreground">v0.1 — hackathon build</div> : undefined}>
      <div className="grid md:grid-cols-12 gap-10 min-h-[70vh]">
        <div className="md:col-span-7 flex flex-col justify-between">
          <HeroMark className="w-full max-w-[640px]" />
          <div className="mt-10 max-w-2xl">
            <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground mb-3">
              (Working Worldwide)
            </p>
            <h1
              className="text-4xl md:text-6xl font-light leading-[1.05] tracking-tight"
              style={{ textWrap: "balance" } as React.CSSProperties}
            >
              Credence<sup className="text-xs ml-1 align-super">®</sup> is a trust-and-fit
              scoring tool for B2B prospects.
            </h1>
            <p className="text-sm text-muted-foreground mt-6 max-w-lg leading-relaxed">
              Triangulate functional scope across LinkedIn, USPTO, GitHub, and
              public conference signal. Every score is reproducible and ships with
              a falsification trail.
            </p>
          </div>
        </div>

        <div className="md:col-span-5 flex flex-col justify-end gap-px">
          <Link
            to="/discover"
            className="group block border border-border p-8 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-6">Pipeline</div>
            <div className="text-2xl md:text-3xl font-light tracking-tight mb-2">
              Browse the prospect graph.
            </div>
            <div className="text-sm text-muted-foreground mb-6">
              Triangulate people across companies, roles, cities, and evidence
              sources. Ask the network to filter, focus, or explain any node.
            </div>
            <div className="flex items-center gap-6 mb-6 text-xs text-mono text-muted-foreground min-h-[16px]">
              {scored.length === 0 && prospects.length === 0 ? (
                <span className="opacity-60">loading network…</span>
              ) : (
                <>
                  <span>
                    {scored.length > 0 ? `${scored.length} scored` : `${prospects.length} prospects`}
                  </span>
                  {companyCount > 0 && <span>{companyCount} companies</span>}
                  {avgScore !== null && (
                    <span style={{ color: scoreColor(avgScore) }}>
                      avg {avgScore.toFixed(1)}
                    </span>
                  )}
                </>
              )}
            </div>
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /discover</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>
          <Link
            to="/validate"
            className="group block border border-border p-6 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-3">Validate</div>
            <div className="text-lg font-light tracking-tight mb-1.5">
              Search a specific lead.
            </div>
            <div className="text-xs text-muted-foreground mb-4">
              Look up a person by name + company + role and rank against existing
              candidates.
            </div>
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /validate</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>
          <Link
            to="/settings"
            className="group block border border-border p-6 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-3">Weights</div>
            <div className="text-lg font-light tracking-tight mb-1.5">
              Tune the scoring model.
            </div>
            <div className="text-xs text-muted-foreground mb-4">
              Edit per-signal contribution to authenticity, authority, and warmth.
              Saving recomputes every prospect immediately.
            </div>
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /settings</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>
        </div>
      </div>
    </PageShell>
  );
};

export default Index;
