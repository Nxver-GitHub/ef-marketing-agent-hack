import { useState, useRef, KeyboardEvent, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { db, useProspects, useScoresFor } from "@/lib/db";
import { useAutocompleteSources, rankSuggestions } from "@/lib/autocompleteSources";
import { scoreColor } from "@/components/ScoreBar";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

type ValidateRanked = {
  id: string;
  name: string;
  company: string;
  role: string;
  industry: string;
  linkedin_url: string | null;
  match_score: number;
  overall_score: number;
  authenticity_score: number;
  authority_score: number;
  warmth_score: number;
  blended: number;
};

const INDUSTRIES = ["Semiconductors", "Defense", "Aerospace", "Health Tech", "Quantum", "Pharma"];

const Validate = () => {
  useDocumentTitle("Validate");
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [roles, setRoles] = useState<string[]>([]);
  const [roleInput, setRoleInput] = useState("");
  const [keywords, setKeywords] = useState<string[]>([]);
  const [keywordInput, setKeywordInput] = useState("");
  const [industry, setIndustry] = useState("Semiconductors");
  const [submitting, setSubmitting] = useState(false);
  const roleInputRef = useRef<HTMLInputElement>(null);
  const keywordInputRef = useRef<HTMLInputElement>(null);
  const [results, setResults] = useState<ValidateRanked[] | null>(null);
  const [resultsQuery, setResultsQuery] = useState<{
    name: string;
    company: string;
    roles: string[];
    keywords: string[];
    industry: string;
  } | null>(null);

  // Pull the unified prospect+score corpus (mock, snapshot, or live Supabase
  // — all resolved by db.ts). Validation ranks against this in-memory set,
  // so the page works in every mode without separate code paths.
  const allProspects = useProspects();
  const allProspectIds = useMemo(() => allProspects.map((p) => p._id), [allProspects]);
  const scoreMap = useScoresFor(allProspectIds);

  const makeTagHandlers = (
    items: string[],
    setItems: React.Dispatch<React.SetStateAction<string[]>>,
    inputValue: string,
    setInputValue: React.Dispatch<React.SetStateAction<string>>,
  ) => ({
    add: (value: string) => {
      const trimmed = value.trim();
      if (trimmed && !items.includes(trimmed)) {
        setItems((prev) => [...prev, trimmed]);
      }
      setInputValue("");
    },
    remove: (item: string) => setItems((prev) => prev.filter((i) => i !== item)),
    onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
        e.preventDefault();
        const trimmed = inputValue.trim();
        if (trimmed && !items.includes(trimmed)) {
          setItems((prev) => [...prev, trimmed]);
        }
        setInputValue("");
      } else if (e.key === "Backspace" && inputValue === "" && items.length > 0) {
        setItems((prev) => prev.slice(0, -1));
      }
    },
  });

  const roleHandlers = makeTagHandlers(roles, setRoles, roleInput, setRoleInput);
  const keywordHandlers = makeTagHandlers(keywords, setKeywords, keywordInput, setKeywordInput);

  const { roles: roleSource, keywords: keywordSource, companies: companySource } = useAutocompleteSources();
  const roleSuggestions = useMemo(
    () => rankSuggestions(roleSource, roleInput, 8).filter((s) => !roles.includes(s)),
    [roleSource, roleInput, roles],
  );
  const keywordSuggestions = useMemo(
    () => rankSuggestions(keywordSource, keywordInput, 8).filter((s) => !keywords.includes(s)),
    [keywordSource, keywordInput, keywords],
  );
  const companySuggestions = useMemo(
    () => rankSuggestions(companySource, company, 8),
    [companySource, company],
  );

  const canSubmit = !submitting && company.trim() && roles.length > 0;

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const finalRoles = roleInput.trim()
      ? [...new Set([...roles, roleInput.trim()])]
      : roles;
    const finalKeywords = keywordInput.trim()
      ? [...new Set([...keywords, keywordInput.trim()])]
      : keywords;
    if (finalRoles.length === 0) return;
    setSubmitting(true);
    const trimmedName = name.trim();
    const companyTrim = company.trim();

    setResultsQuery({
      name: trimmedName,
      company: companyTrim,
      roles: finalRoles,
      keywords: finalKeywords,
      industry,
    });

    const top10 = lookupTopProspectsLocal({
      name: trimmedName,
      company: companyTrim,
      roles: finalRoles,
      keywords: finalKeywords,
      industry,
    });
    setResults(top10);
    setSubmitting(false);
  };

  const resetToForm = () => {
    setResults(null);
    setResultsQuery(null);
  };

  const createStubFromQuery = async () => {
    if (!resultsQuery) return;
    setSubmitting(true);
    const { name: n, company: c, roles: rs, keywords: kw, industry: ind } = resultsQuery;
    const synthesizedName = n || `${rs[0]} at ${c}`;
    const id = await db.createProspect({
      name: synthesizedName,
      company: c,
      role: rs[0],
      roles: rs,
      keywords: kw,
      industry: ind,
    });
    void db.runScoring(id);
    navigate(`/prospect/${id}`);
  };

  // Rank top-10 prospects matching (name?, company, roles[], keywords[], industry)
  // against the unified in-memory corpus (mock, snapshot, or live Supabase).
  //
  // Rubric (out of ~10):
  //   +4 if name matches (only when user provided a name)
  //   +3 weighted by role-token overlap ratio (union of all role tags)
  //   +2 if company matches (substring, either direction)
  //   +1 if industry matches
  //   +0.5 per keyword that appears in prospect.role (up to +2)
  // Blended rank = 0.6 * match_score + 0.4 * (overall_score / 10)
  function lookupTopProspectsLocal(q: {
    name: string;
    company: string;
    roles: string[];
    keywords: string[];
    industry: string;
  }): ValidateRanked[] {
    if (allProspects.length === 0) return [];
    const roleTokens = new Set<string>();
    for (const rl of q.roles) {
      for (const t of rl.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)) roleTokens.add(t);
    }
    const nameLower = q.name.toLowerCase();
    const companyLower = q.company.toLowerCase();
    const industryLower = q.industry.toLowerCase();
    const kwLowers = q.keywords.map((k) => k.toLowerCase()).filter(Boolean);

    const ranked: ValidateRanked[] = [];
    for (const p of allProspects) {
      const pCompany = (p.company ?? "").toLowerCase();
      const pIndustry = (p.industry ?? "").toLowerCase();
      const pRole = (p.role ?? "").toLowerCase();
      const pName = (p.name ?? "").toLowerCase();

      let s = 0;
      if (q.name && pName.includes(nameLower)) s += 4;
      const rRoleTokens = new Set(pRole.split(/[^a-z0-9]+/).filter(Boolean));
      let overlap = 0;
      for (const t of roleTokens) if (rRoleTokens.has(t)) overlap += 1;
      if (overlap > 0) s += 3 * (overlap / Math.max(1, roleTokens.size));
      if (companyLower && (pCompany.includes(companyLower) || companyLower.includes(pCompany))) s += 2;
      if (pIndustry.includes(industryLower)) s += 1;
      let kwHits = 0;
      for (const kw of kwLowers) if (kw && pRole.includes(kw)) kwHits += 1;
      s += Math.min(2, kwHits * 0.5);

      const score = scoreMap[p._id];
      const overall = score?.overall_score ?? 0;
      const blended = 0.6 * s + 0.4 * (overall / 10);

      ranked.push({
        id: p._id,
        name: p.name ?? "",
        company: p.company ?? "",
        role: p.role ?? "",
        industry: p.industry ?? "",
        linkedin_url: (p as { linkedin_url?: string | null }).linkedin_url ?? null,
        match_score: Math.round(s * 10) / 10,
        overall_score: overall,
        authenticity_score: score?.authenticity_score ?? 0,
        authority_score: score?.authority_score ?? 0,
        warmth_score: score?.warmth_score ?? 0,
        blended,
      });
    }
    ranked.sort((a, b) => b.blended - a.blended);
    // Minimum match-fit gate: don't show candidates scored purely on overall
    // while they have zero input-match. Requires at least company OR role overlap.
    return ranked.filter((r) => r.match_score >= 2).slice(0, 10);
  }

  if (results !== null && resultsQuery) {
    return (
      <PageShell>
        <div className="mb-8 flex items-end justify-between">
          <div>
            <div className="label-eyebrow mb-2">Flow 01 · Results</div>
            <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05]">
              Top {results.length} candidate{results.length === 1 ? "" : "s"}
            </h1>
            <div className="text-sm text-muted-foreground mt-3 flex flex-wrap gap-x-3 gap-y-1">
              <span>{resultsQuery.industry}</span>
              <span className="text-muted-foreground/40">·</span>
              <span>{resultsQuery.company || "any company"}</span>
              <span className="text-muted-foreground/40">·</span>
              <span>{resultsQuery.roles.join(" / ") || "any role"}</span>
              {resultsQuery.keywords.length > 0 && (
                <>
                  <span className="text-muted-foreground/40">·</span>
                  <span>{resultsQuery.keywords.join(", ")}</span>
                </>
              )}
            </div>
          </div>
          <button
            onClick={resetToForm}
            className="text-xs text-mono text-muted-foreground hover:text-foreground border border-border px-3 py-2 transition-colors"
          >
            ← Refine search
          </button>
        </div>

        {results.length === 0 ? (
          <div className="border border-border p-8 text-center space-y-4">
            <div className="text-sm text-muted-foreground">
              No existing prospects match this query.
            </div>
            <button
              onClick={createStubFromQuery}
              disabled={submitting}
              className="border border-foreground bg-foreground text-background px-5 py-2.5 text-sm hover:opacity-80 transition-opacity disabled:opacity-40"
            >
              {submitting ? "Creating…" : "Create new prospect & score"}
            </button>
          </div>
        ) : (
          <div className="border border-border">
            <div className="grid grid-cols-12 px-4 py-3 text-[10px] uppercase tracking-[0.16em] text-muted-foreground border-b border-border items-center">
              <div className="col-span-1">#</div>
              <div className="col-span-3">Name / Company</div>
              <div className="col-span-3">Role</div>
              <div className="col-span-1 text-right">Match</div>
              <div className="col-span-1 text-right">Auth</div>
              <div className="col-span-1 text-right">Athy</div>
              <div className="col-span-1 text-right">Wrm</div>
              <div className="col-span-1 text-right">Score</div>
            </div>
            {results.map((r, i) => (
              <button
                key={r.id}
                onClick={() => navigate(`/prospect/${r.id}`)}
                className="w-full grid grid-cols-12 items-center px-4 py-4 border-b border-border/60 last:border-0 text-left hover:bg-secondary transition-colors"
              >
                <div className="col-span-1 text-mono text-xs text-muted-foreground">
                  {String(i + 1).padStart(2, "0")}
                </div>
                <div className="col-span-3">
                  <div className="text-sm">{r.name}</div>
                  <div className="text-xs text-muted-foreground">{r.company}</div>
                </div>
                <div className="col-span-3 text-xs text-muted-foreground truncate pr-4">
                  {r.role}
                </div>
                <div className="col-span-1 text-right text-mono text-xs text-muted-foreground">
                  {r.match_score.toFixed(1)}
                </div>
                <Pill v={r.authenticity_score} />
                <Pill v={r.authority_score} />
                <Pill v={r.warmth_score} />
                <div
                  className="col-span-1 text-right text-mono text-base"
                  style={{ color: scoreColor(r.overall_score) }}
                >
                  {Math.round(r.overall_score)}
                </div>
              </button>
            ))}
          </div>
        )}
      </PageShell>
    );
  }

  return (
    <PageShell>
      <div className="grid md:grid-cols-12 gap-10">
        <div className="md:col-span-5">
          <div className="label-eyebrow mb-3">Flow 01</div>
          <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05] mb-6">
            Find the right person to contact.
          </h1>
          <p className="text-sm text-muted-foreground max-w-md leading-relaxed">
            You know the role, the company, and the domain — but not the name.
            Describe what you know and we will surface the exact lead, scored
            and falsifiable.
          </p>
          <div className="mt-6 text-[10px] text-mono uppercase tracking-[0.16em] text-muted-foreground">
            {allProspects.length === 0
              ? "loading prospect index…"
              : `${allProspects.length.toLocaleString()} prospects indexed`}
          </div>
        </div>

        <form onSubmit={onSubmit} className="md:col-span-7 space-y-px">
          <Field
            label="Name"
            value={name}
            onChange={setName}
            placeholder="Jane Chen (optional)"
          />
          <SuggestField
            label="Company"
            value={company}
            onChange={setCompany}
            placeholder="ASML"
            suggestions={companySuggestions}
            onPickSuggestion={setCompany}
          />

          {/* Multi-role tag input */}
          <TagInputField
            label="Role"
            items={roles}
            inputValue={roleInput}
            inputRef={roleInputRef}
            onInputChange={setRoleInput}
            onKeyDown={roleHandlers.onKeyDown}
            onBlur={() => { if (roleInput.trim()) roleHandlers.add(roleInput); }}
            onRemove={roleHandlers.remove}
            placeholder={roles.length === 0 ? "VP Lithography" : "Add another role…"}
            hint="Press Enter or comma to add. Multiple roles narrow the search."
            suggestions={roleSuggestions}
            onPickSuggestion={(s) => roleHandlers.add(s)}
          />

          {/* Keywords / descriptors */}
          <TagInputField
            label="Keywords"
            items={keywords}
            inputValue={keywordInput}
            inputRef={keywordInputRef}
            onInputChange={setKeywordInput}
            onKeyDown={keywordHandlers.onKeyDown}
            onBlur={() => { if (keywordInput.trim()) keywordHandlers.add(keywordInput); }}
            onRemove={keywordHandlers.remove}
            placeholder={keywords.length === 0 ? "chip manufacturing, NPI, wafer fab…" : "Add keyword…"}
            hint="Domain terms, team focus, or product areas that describe this person's work."
            tagStyle="secondary"
            suggestions={keywordSuggestions}
            onPickSuggestion={(s) => keywordHandlers.add(s)}
          />

          <div className="border border-border p-5">
            <div className="label-eyebrow mb-3">Industry</div>
            <div className="flex flex-wrap gap-2">
              {INDUSTRIES.map((i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => setIndustry(i)}
                  className={`text-xs px-3 py-1.5 border transition-colors ${
                    industry === i
                      ? "border-foreground bg-foreground text-background"
                      : "border-border text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {i}
                </button>
              ))}
            </div>
          </div>

          <button
            type="submit"
            disabled={!canSubmit}
            aria-busy={submitting}
            className="relative w-full text-left border border-border p-5 mt-px hover:bg-secondary transition-colors disabled:opacity-40 disabled:cursor-not-allowed group overflow-hidden"
          >
            <div className="flex items-center justify-between">
              <div>
                <div className="label-eyebrow mb-1.5">
                  {submitting ? "Scoring…" : "Submit"}
                </div>
                <div className="text-2xl font-light tracking-tight">
                  {submitting ? "Searching evidence sources…" : "Find & score the lead →"}
                </div>
              </div>
              <div
                className={`w-10 h-10 rounded-full ${submitting ? "animate-pulse" : ""}`}
                style={{ background: "hsl(var(--accent))" }}
              />
            </div>
            {submitting && (
              <div
                aria-hidden="true"
                className="absolute left-0 bottom-0 h-0.5 w-full bg-foreground/30 overflow-hidden"
              >
                <div className="h-full w-1/3 bg-foreground/80 animate-pulse" />
              </div>
            )}
          </button>
        </form>
      </div>
    </PageShell>
  );
};

interface TagInputFieldProps {
  label: string;
  items: string[];
  inputValue: string;
  inputRef: React.RefObject<HTMLInputElement>;
  onInputChange: (v: string) => void;
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
  onBlur: () => void;
  onRemove: (item: string) => void;
  placeholder?: string;
  hint?: string;
  tagStyle?: "primary" | "secondary";
  suggestions?: string[];
  onPickSuggestion?: (value: string) => void;
}

const TagInputField = ({
  label,
  items,
  inputValue,
  inputRef,
  onInputChange,
  onKeyDown,
  onBlur,
  onRemove,
  placeholder,
  hint,
  tagStyle = "primary",
  suggestions = [],
  onPickSuggestion,
}: TagInputFieldProps) => {
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  // Trigger suggestions when: input focused + user is actively typing (not
  // just-dismissed) AND there's something to show. `dismissed` resets whenever
  // the input value changes so suggestions re-open on the next keystroke.
  const showSuggestions =
    focused && !dismissed && suggestions.length > 0 && !!onPickSuggestion;

  // Enter/Tab priority: if suggestions are showing AND user's raw input doesn't
  // match any existing tag AND first suggestion starts-with the input, inject
  // it instead of the raw input. Wraps the original onKeyDown.
  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (
      showSuggestions &&
      (e.key === "Enter" || e.key === "Tab") &&
      inputValue.trim() &&
      suggestions[0]?.toLowerCase().startsWith(inputValue.trim().toLowerCase()) &&
      onPickSuggestion
    ) {
      e.preventDefault();
      onPickSuggestion(suggestions[0]);
      setDismissed(true);
      return;
    }
    if (e.key === "Escape") {
      setDismissed(true);
      return;
    }
    onKeyDown(e);
  };

  return (
    <div
      className="border border-border p-5 cursor-text relative"
      onClick={() => inputRef.current?.focus()}
    >
      <div className="label-eyebrow mb-3">{label}</div>
      <div className="flex flex-wrap gap-2 mb-2">
        {items.map((item) => (
          <span
            key={item}
            className={`flex items-center gap-1.5 text-xs px-3 py-1.5 border transition-colors ${
              tagStyle === "primary"
                ? "border-foreground bg-foreground text-background"
                : "border-border text-foreground bg-secondary"
            }`}
          >
            {item}
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onRemove(item); }}
              className="opacity-60 hover:opacity-100 transition-opacity leading-none"
              aria-label={`Remove ${item}`}
            >
              ×
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          value={inputValue}
          onChange={(e) => {
            onInputChange(e.target.value);
            setDismissed(false);
          }}
          onKeyDown={handleKey}
          onFocus={() => {
            setFocused(true);
            setDismissed(false);
          }}
          onBlur={() => {
            // Delay so click on a suggestion row fires before blur hides the menu.
            setTimeout(() => setFocused(false), 120);
            onBlur();
          }}
          placeholder={placeholder}
          className="bg-transparent outline-none text-2xl font-light tracking-tight placeholder:text-muted-foreground/40 flex-1 min-w-[180px]"
        />
      </div>
      {items.length === 0 && hint && (
        <p className="text-xs text-muted-foreground/50 mt-1">{hint}</p>
      )}
      {showSuggestions && (
        <div
          className="absolute left-0 right-0 top-full z-20 border border-t-0 border-border bg-background shadow-lg"
          // prevent the input blur from firing before our click registers
          onMouseDown={(e) => e.preventDefault()}
        >
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => {
                onPickSuggestion?.(s);
                setDismissed(true);
              }}
              className="w-full text-left px-5 py-2.5 text-sm hover:bg-secondary transition-colors border-b border-border/40 last:border-b-0"
            >
              <Highlight text={s} query={inputValue} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

const Highlight = ({ text, query }: { text: string; query: string }) => {
  const q = query.trim();
  if (!q) return <>{text}</>;
  const lower = text.toLowerCase();
  const idx = lower.indexOf(q.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <span className="text-foreground font-medium">{text.slice(idx, idx + q.length)}</span>
      {text.slice(idx + q.length)}
    </>
  );
};

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
  <label className="block border border-border p-5">
    <div className="label-eyebrow mb-2">{label}</div>
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full bg-transparent outline-none text-2xl font-light tracking-tight placeholder:text-muted-foreground/40"
    />
  </label>
);

const Pill = ({ v }: { v: number }) => (
  <div
    className="col-span-1 text-right text-mono text-xs"
    style={{ color: scoreColor(v) }}
  >
    {Math.round(v)}
  </div>
);

const SuggestField = ({
  label,
  value,
  onChange,
  placeholder,
  suggestions = [],
  onPickSuggestion,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  suggestions?: string[];
  onPickSuggestion?: (v: string) => void;
}) => {
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const showSuggestions = focused && !dismissed && suggestions.length > 0 && !!value.trim();

  return (
    <div className="relative border border-border p-5">
      <div className="label-eyebrow mb-2">{label}</div>
      <input
        value={value}
        onChange={(e) => { onChange(e.target.value); setDismissed(false); }}
        onFocus={() => { setFocused(true); setDismissed(false); }}
        onBlur={() => setTimeout(() => setFocused(false), 120)}
        onKeyDown={(e) => { if (e.key === "Escape") setDismissed(true); }}
        placeholder={placeholder}
        className="w-full bg-transparent outline-none text-2xl font-light tracking-tight placeholder:text-muted-foreground/40"
      />
      {showSuggestions && (
        <div
          className="absolute left-0 right-0 top-full z-20 border border-t-0 border-border bg-background shadow-lg"
          onMouseDown={(e) => e.preventDefault()}
        >
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => { onPickSuggestion?.(s); setDismissed(true); }}
              className="w-full text-left px-5 py-2.5 text-sm hover:bg-secondary transition-colors border-b border-border/40 last:border-b-0"
            >
              <Highlight text={s} query={value} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export default Validate;
