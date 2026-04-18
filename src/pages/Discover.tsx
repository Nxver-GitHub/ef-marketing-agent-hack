import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { useProspects, useScoresFor } from "@/lib/db";
import { scoreColor } from "@/components/ScoreBar";

const ROLES = ["Engineering Lead", "Director", "VP", "Principal", "C-Suite"];

const Discover = () => {
  const navigate = useNavigate();
  const prospects = useProspects();
  const scores = useScoresFor(prospects.map((p) => p._id));

  const [industry, setIndustry] = useState("Semiconductors");
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [submitted, setSubmitted] = useState(true); // start with results visible

  const ranked = useMemo(() => {
    let list = prospects.filter((p) => p.industry === industry);
    if (company) list = list.filter((p) => p.company.toLowerCase().includes(company.toLowerCase()));
    if (role) list = list.filter((p) => p.role.toLowerCase().includes(role.toLowerCase()));
    return list
      .map((p) => ({ p, score: scores[p._id] }))
      .filter((r) => r.score)
      .sort((a, b) => (b.score?.overall_score ?? 0) - (a.score?.overall_score ?? 0));
  }, [prospects, scores, industry, company, role]);

  return (
    <PageShell>
      <div className="grid md:grid-cols-12 gap-10">
        <div className="md:col-span-4">
          <div className="label-eyebrow mb-3">Flow 02</div>
          <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05] mb-6">
            Find ICP matches.
          </h1>
          <p className="text-sm text-muted-foreground mb-8">
            Define an ideal-customer profile. Get a ranked list of prospects, scored on the same
            transparent rubric as Flow 01.
          </p>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              setSubmitted(true);
            }}
            className="space-y-px"
          >
            <Field label="Industry" value={industry} onChange={setIndustry} />
            <Field label="Company (optional)" value={company} onChange={setCompany} placeholder="any" />
            <Field label="Target role" value={role} onChange={setRole} placeholder="VP, Director…" />
            <button className="w-full text-left border border-border p-4 hover:bg-secondary transition-colors text-sm">
              Run match →
            </button>
            <div className="pt-2 text-[11px] text-muted-foreground">
              + Add filter (TODO: extra criteria)
            </div>
          </form>
        </div>

        <div className="md:col-span-8">
          <div className="flex items-baseline justify-between mb-4">
            <div className="label-eyebrow">Results</div>
            <div className="text-mono text-xs text-muted-foreground">
              {ranked.length} matches
            </div>
          </div>
          <div className="border border-border">
            <div className="grid grid-cols-12 px-4 py-3 text-[10px] uppercase tracking-[0.16em] text-muted-foreground border-b border-border">
              <div className="col-span-1">#</div>
              <div className="col-span-4">Name / Company</div>
              <div className="col-span-3">Role</div>
              <div className="col-span-1 text-right">Auth</div>
              <div className="col-span-1 text-right">Athy</div>
              <div className="col-span-1 text-right">Wrm</div>
              <div className="col-span-1 text-right">Score</div>
            </div>
            {submitted && ranked.length === 0 && (
              <div className="p-6 text-sm text-muted-foreground">No matches.</div>
            )}
            {ranked.map(({ p, score }, i) => (
              <button
                key={p._id}
                onClick={() => navigate(`/prospect/${p._id}`)}
                className="w-full grid grid-cols-12 items-center px-4 py-4 border-b border-border/60 last:border-0 text-left hover:bg-secondary transition-colors"
              >
                <div className="col-span-1 text-mono text-xs text-muted-foreground">
                  {String(i + 1).padStart(2, "0")}
                </div>
                <div className="col-span-4">
                  <div className="text-sm">{p.name}</div>
                  <div className="text-xs text-muted-foreground">{p.company}</div>
                </div>
                <div className="col-span-3 text-xs text-muted-foreground">{p.role}</div>
                <Pill v={score!.authenticity_score} />
                <Pill v={score!.authority_score} />
                <Pill v={score!.warmth_score} />
                <div
                  className="col-span-1 text-right text-mono text-base"
                  style={{ color: scoreColor(score!.overall_score) }}
                >
                  {Math.round(score!.overall_score)}
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
    </PageShell>
  );
};

const Pill = ({ v }: { v: number }) => (
  <div className="col-span-1 text-right text-mono text-xs" style={{ color: scoreColor(v) }}>
    {Math.round(v)}
  </div>
);

const Field = ({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) => (
  <label className="block border border-border p-4">
    <div className="label-eyebrow mb-1.5">{label}</div>
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full bg-transparent outline-none text-lg font-light tracking-tight placeholder:text-muted-foreground/40"
    />
  </label>
);

export default Discover;
