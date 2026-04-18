import { Link } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { HeroMark } from "@/components/HeroMark";
import { useProspects, useScoresFor } from "@/lib/db";
import { useMemo } from "react";
import { scoreColor } from "@/components/ScoreBar";

const Index = () => {
  const prospects = useProspects();
  const scores = useScoresFor(prospects.map((p) => p._id));
  const scored = useMemo(() => prospects.filter((p) => scores[p._id]), [prospects, scores]);
  const avgScore = scored.length
    ? scored.reduce((s, p) => s + (scores[p._id]?.overall_score ?? 0), 0) / scored.length
    : null;

  return (
    <PageShell rightSlot={<div className="text-mono text-[10px] text-muted-foreground">v0.1 — hackathon build</div>}>
      <div className="grid md:grid-cols-12 gap-10 min-h-[70vh]">
        <div className="md:col-span-7 flex flex-col justify-between">
          <HeroMark className="w-full max-w-[640px]" />
          <div className="mt-10 max-w-2xl">
            <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground mb-3">
              (Working Worldwide)
            </p>
            <h1 className="text-4xl md:text-6xl font-light leading-[1.05] tracking-tight">
              Credence<sup className="text-xs ml-1 align-super">®</sup> is a
              <br />
              trust-and-fit scoring tool
              <br />
              for B2B prospects.
            </h1>
          </div>
        </div>

        <div className="md:col-span-5 flex flex-col justify-end gap-px">
          <Link
            to="/validate"
            className="group block border border-border p-8 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-6">Validate</div>
            <div className="text-2xl md:text-3xl font-light tracking-tight mb-2">
              Score a prospect.
            </div>
            <div className="text-sm text-muted-foreground mb-10">
              Enter a name, company, role, and industry. Get a transparent trust-and-fit score with
              every contributing signal exposed and falsifiable.
            </div>
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /validate</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>

          <Link
            to="/discover"
            className="group block border border-border border-t-0 p-8 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-6">Pipeline</div>
            <div className="text-2xl md:text-3xl font-light tracking-tight mb-2">
              Browse scored prospects.
            </div>
            <div className="text-sm text-muted-foreground mb-6">
              Filter, rank, and compare every prospect that has run through the scoring engine.
            </div>
            {scored.length > 0 && (
              <div className="flex items-center gap-6 mb-6 text-xs text-mono text-muted-foreground">
                <span>{scored.length} scored</span>
                {avgScore !== null && (
                  <span style={{ color: scoreColor(avgScore) }}>
                    avg {avgScore.toFixed(1)}
                  </span>
                )}
              </div>
            )}
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /discover</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>

          <Link
            to="/settings"
            className="group block border border-border border-t-0 p-8 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-6">Weights</div>
            <div className="text-2xl md:text-3xl font-light tracking-tight mb-2">
              Tune the scoring model.
            </div>
            <div className="text-sm text-muted-foreground mb-10">
              Adjust how each signal contributes to Authenticity, Authority, and Warmth.
              Changes recompute all scores immediately.
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
