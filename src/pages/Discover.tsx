import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { useProspects, useScoresFor, useSignalsForMany, extractSignalValue } from "@/lib/db";
import { scoreColor } from "@/components/ScoreBar";

type SortKey = "overall_score" | "authenticity_score" | "authority_score" | "warmth_score";

const INDUSTRIES = ["All", "Semiconductors", "Defense", "Pharma", "Quantum", "Aerospace"];

const Discover = () => {
  const navigate = useNavigate();
  const prospects = useProspects();
  const prospectIds = useMemo(() => prospects.map((p) => p._id), [prospects]);
  const scores = useScoresFor(prospectIds);
  const signalsById = useSignalsForMany(prospectIds);

  // Pre-compute a compact per-row enrichment line: tenure · talks · patents
  // and an outbound LinkedIn click-through. Numbers come from the signals
  // table (each signal_type stored as {raw: N}). Falls back gracefully when
  // a given signal is missing for a prospect.
  const enrichmentById = useMemo(() => {
    const out: Record<
      string,
      { tenure: number; talks: number; patents: number; mutuals: number }
    > = {};
    for (const pid of Object.keys(signalsById)) {
      const sigs = signalsById[pid] ?? [];
      const pick = (t: string) => {
        const s = sigs.find((s: { signal_type: string }) => s.signal_type === t);
        return s ? extractSignalValue(s.value) : 0;
      };
      out[pid] = {
        tenure: pick("tenure_years"),
        talks: pick("conference_talks"),
        patents: pick("patent_count"),
        mutuals: pick("mutual_connections"),
      };
    }
    return out;
  }, [signalsById]);

  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState("All");
  const [sortKey, setSortKey] = useState<SortKey>("overall_score");

  // Give Supabase a beat before showing "no prospects yet" so the user isn't
  // told to go validate when the data is just mid-flight.
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    if (prospects.length > 0) {
      setSettled(true);
      return;
    }
    const t = setTimeout(() => setSettled(true), 600);
    return () => clearTimeout(t);
  }, [prospects.length]);

  const scored = useMemo(
    () => prospects.filter((p) => scores[p._id]),
    [prospects, scores]
  );
  const avgScore = scored.length
    ? scored.reduce((s, p) => s + (scores[p._id]?.overall_score ?? 0), 0) / scored.length
    : 0;
  const topScore = scored.length
    ? Math.max(...scored.map((p) => scores[p._id]?.overall_score ?? 0))
    : 0;

  // Case-insensitive industry match — DB has "Semiconductors" but API lowercases.
  const industryEq = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return prospects
      .filter((p) => {
        const matchQ =
          !q ||
          p.name.toLowerCase().includes(q) ||
          p.company.toLowerCase().includes(q) ||
          p.role.toLowerCase().includes(q);
        const matchI = industry === "All" || industryEq(p.industry, industry);
        return matchQ && matchI && scores[p._id];
      })
      .map((p) => ({ p, score: scores[p._id]! }))
      .sort((a, b) => (b.score[sortKey] ?? 0) - (a.score[sortKey] ?? 0));
  }, [prospects, scores, query, industry, sortKey]);

  // Per-industry prospect counts (scored only) — used to hint which filters have data.
  const industryCounts = useMemo(() => {
    const counts: Record<string, number> = { All: 0 };
    for (const p of prospects) {
      if (!scores[p._id]) continue;
      counts.All += 1;
      for (const i of INDUSTRIES) {
        if (i !== "All" && industryEq(p.industry, i)) counts[i] = (counts[i] ?? 0) + 1;
      }
    }
    return counts;
  }, [prospects, scores]);

  // Explicit col-span map so Tailwind's JIT statically picks the classes up.
  const colSpanClass: Record<number, string> = { 1: "col-span-1", 2: "col-span-2" };

  const col = (key: SortKey, label: string, span = 1) => (
    <button
      onClick={() => setSortKey(key)}
      className={`${colSpanClass[span] ?? "col-span-1"} text-right text-[10px] uppercase tracking-[0.16em] transition-colors ${
        sortKey === key ? "text-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
      {sortKey === key && <span className="ml-1 text-mono">↓</span>}
    </button>
  );

  return (
    <PageShell>
      {/* Stats */}
      <div className="grid grid-cols-3 gap-px border border-border mb-8">
        <Stat label="Total prospects" value={String(prospects.length)} />
        <Stat
          label="Avg score"
          value={scored.length ? avgScore.toFixed(1) : "—"}
          color={scored.length ? scoreColor(avgScore) : undefined}
        />
        <Stat
          label="Top score"
          value={scored.length ? String(Math.round(topScore)) : "—"}
          color={scored.length ? scoreColor(topScore) : undefined}
        />
      </div>

      {/* Search + industry filter */}
      <div className="flex flex-col md:flex-row gap-px mb-px">
        <label className="border border-border flex-1 flex items-center px-4 gap-3 py-2">
          <span className="text-muted-foreground text-[10px] uppercase tracking-[0.16em]">
            Search
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Name, company, or role…"
            className="flex-1 bg-transparent outline-none text-sm placeholder:text-muted-foreground/40"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="text-muted-foreground hover:text-foreground text-xs"
            >
              ×
            </button>
          )}
        </label>
        <div className="border border-border md:border-l-0 flex items-center px-4 gap-1.5 flex-wrap py-2">
          {INDUSTRIES.map((i) => {
            const n = industryCounts[i] ?? 0;
            const hasData = i === "All" ? n > 0 : n > 0;
            const active = industry === i;
            return (
              <button
                key={i}
                onClick={() => setIndustry(i)}
                disabled={!hasData && !active}
                className={`text-[10px] px-2.5 py-1 border transition-colors inline-flex items-center gap-1.5 ${
                  active
                    ? "border-foreground bg-foreground text-background"
                    : hasData
                      ? "border-transparent text-muted-foreground hover:text-foreground"
                      : "border-transparent text-muted-foreground/30 cursor-not-allowed"
                }`}
                title={hasData ? `${n} prospect${n !== 1 ? "s" : ""}` : "no data yet"}
              >
                <span>{i}</span>
                {n > 0 && (
                  <span className={`text-mono ${active ? "text-background/70" : "text-muted-foreground/60"}`}>
                    {n}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* Table */}
      <div className="border border-border border-t-0">
        <div className="grid grid-cols-12 gap-2 px-4 py-3 text-[10px] uppercase tracking-[0.16em] text-muted-foreground border-b border-border items-center">
          <div className="col-span-1">#</div>
          <div className="col-span-3">Name / Company</div>
          <div className="col-span-3">Role</div>
          {col("authenticity_score", "Authentic")}
          {col("authority_score", "Authority")}
          {col("warmth_score", "Warmth")}
          {col("overall_score", "Overall", 2)}
        </div>

        {filtered.length === 0 && (
          <div className="px-4 py-12 text-sm text-muted-foreground">
            {!settled
              ? "Loading prospects…"
              : prospects.length === 0
                ? "No prospects yet — validate one to get started."
                : scored.length === 0
                  ? "Prospects loaded but not scored yet — scoring is still running."
                  : "No matches for this filter."}
          </div>
        )}

        {filtered.map(({ p, score }, i) => (
          <button
            key={p._id}
            onClick={() => navigate(`/prospect/${p._id}`)}
            className="w-full grid grid-cols-12 gap-2 items-center px-4 py-4 border-b border-border/60 last:border-0 text-left hover:bg-secondary transition-colors"
          >
            <div className="col-span-1 text-mono text-xs text-muted-foreground">
              {String(i + 1).padStart(2, "0")}
            </div>
            <div className="col-span-3 min-w-0">
              <div className="text-sm truncate" title={p.name}>
                {p.name}
              </div>
              <div className="text-xs text-muted-foreground truncate" title={p.company}>
                {p.company}
              </div>
            </div>
            <div
              className="col-span-3 text-xs text-muted-foreground pr-4 leading-snug"
              title={p.role}
            >
              <div className="line-clamp-2">{p.role}</div>
              <Enrichment
                signals={enrichmentById[p._id]}
                linkedinUrl={p.linkedin_url ?? undefined}
              />
            </div>
            <Pill v={score.authenticity_score} />
            <Pill v={score.authority_score} />
            <Pill v={score.warmth_score} />
            <div
              className="col-span-2 text-right text-mono text-base"
              style={{ color: scoreColor(score.overall_score) }}
            >
              {Math.round(score.overall_score)}
            </div>
          </button>
        ))}
      </div>

      {filtered.length > 0 && (
        <div className="pt-3 text-[11px] text-muted-foreground text-mono flex items-center justify-between">
          <span>
            {filtered.length} of {scored.length} prospect{scored.length !== 1 ? "s" : ""} · sorted by{" "}
            {sortKey.replace("_score", "")}
          </span>
          {filtered.length < scored.length && (
            <button
              onClick={() => {
                setQuery("");
                setIndustry("All");
              }}
              className="text-[10px] uppercase tracking-[0.16em] hover:text-foreground"
            >
              Clear filters
            </button>
          )}
        </div>
      )}
    </PageShell>
  );
};

const Stat = ({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) => (
  <div className="p-5">
    <div className="label-eyebrow mb-2">{label}</div>
    <div
      className="text-3xl font-light tracking-tight text-mono"
      style={color ? { color } : undefined}
    >
      {value}
    </div>
  </div>
);

const Pill = ({ v }: { v: number }) => (
  <div
    className="col-span-1 text-right text-mono text-xs"
    style={{ color: scoreColor(v) }}
  >
    {Math.round(v)}
  </div>
);

/**
 * Compact per-row enrichment line: tenure · talks · patents · mutuals · LinkedIn.
 * Hidden when no signals have landed yet (prevents visual clutter on cold rows).
 */
const Enrichment = ({
  signals,
  linkedinUrl,
}: {
  signals?: { tenure: number; talks: number; patents: number; mutuals: number };
  linkedinUrl?: string;
}) => {
  const parts: React.ReactNode[] = [];
  if (signals?.tenure) parts.push(<span key="tenure">{Math.round(signals.tenure)}y @ co</span>);
  if (signals?.talks) parts.push(<span key="talks">{Math.round(signals.talks)} talks</span>);
  if (signals?.patents) parts.push(<span key="patents">{Math.round(signals.patents)} patents</span>);
  if (signals?.mutuals) parts.push(<span key="mut">{Math.round(signals.mutuals)} mutuals</span>);
  if (parts.length === 0 && !linkedinUrl) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-1 text-[10px] text-muted-foreground/70">
      {parts.map((p, i) => (
        <span key={i} className="after:content-['·'] after:ml-2 after:text-muted-foreground/30 last:after:content-['']">
          {p}
        </span>
      ))}
      {linkedinUrl && (
        <a
          href={linkedinUrl}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="underline hover:text-foreground"
        >
          → LinkedIn
        </a>
      )}
    </div>
  );
};

export default Discover;
