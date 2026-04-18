import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { useProspects, useScoresFor } from "@/lib/db";
import { scoreColor } from "@/components/ScoreBar";

type SortKey = "overall_score" | "authenticity_score" | "authority_score" | "warmth_score";

const INDUSTRIES = ["All", "Semiconductors", "Defense", "Pharma", "Quantum", "Aerospace"];

const Discover = () => {
  const navigate = useNavigate();
  const prospects = useProspects();
  const scores = useScoresFor(prospects.map((p) => p._id));

  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState("All");
  const [sortKey, setSortKey] = useState<SortKey>("overall_score");

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

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return prospects
      .filter((p) => {
        const matchQ =
          !q ||
          p.name.toLowerCase().includes(q) ||
          p.company.toLowerCase().includes(q) ||
          p.role.toLowerCase().includes(q);
        const matchI = industry === "All" || p.industry === industry;
        return matchQ && matchI && scores[p._id];
      })
      .map((p) => ({ p, score: scores[p._id]! }))
      .sort((a, b) => (b.score[sortKey] ?? 0) - (a.score[sortKey] ?? 0));
  }, [prospects, scores, query, industry, sortKey]);

  const col = (key: SortKey, label: string, span = 1) => (
    <button
      onClick={() => setSortKey(key)}
      className={`col-span-${span} text-right text-[10px] uppercase tracking-[0.16em] transition-colors ${
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
          {INDUSTRIES.map((i) => (
            <button
              key={i}
              onClick={() => setIndustry(i)}
              className={`text-[10px] px-2.5 py-1 border transition-colors ${
                industry === i
                  ? "border-foreground bg-foreground text-background"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              {i}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="border border-border border-t-0">
        <div className="grid grid-cols-12 px-4 py-3 text-[10px] uppercase tracking-[0.16em] text-muted-foreground border-b border-border items-center">
          <div className="col-span-1">#</div>
          <div className="col-span-3">Name / Company</div>
          <div className="col-span-3">Role</div>
          {col("authenticity_score", "Auth")}
          {col("authority_score", "Athy")}
          {col("warmth_score", "Wrm")}
          {col("overall_score", "Score", 2)}
        </div>

        {filtered.length === 0 && (
          <div className="px-4 py-12 text-sm text-muted-foreground">
            {prospects.length === 0
              ? "No prospects yet — validate one to get started."
              : "No matches for this filter."}
          </div>
        )}

        {filtered.map(({ p, score }, i) => (
          <button
            key={p._id}
            onClick={() => navigate(`/prospect/${p._id}`)}
            className="w-full grid grid-cols-12 items-center px-4 py-4 border-b border-border/60 last:border-0 text-left hover:bg-secondary transition-colors"
          >
            <div className="col-span-1 text-mono text-xs text-muted-foreground">
              {String(i + 1).padStart(2, "0")}
            </div>
            <div className="col-span-3">
              <div className="text-sm">{p.name}</div>
              <div className="text-xs text-muted-foreground">{p.company}</div>
            </div>
            <div className="col-span-3 text-xs text-muted-foreground truncate pr-4">
              {p.role}
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
        <div className="pt-3 text-[11px] text-muted-foreground text-mono">
          {filtered.length} prospect{filtered.length !== 1 ? "s" : ""} · sorted by{" "}
          {sortKey.replace("_score", "").replace("_", " ")}
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

export default Discover;
