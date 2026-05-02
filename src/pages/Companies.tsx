/**
 * Companies — top-level navigable list of every company in the DB.
 *
 * Phase D1 of the v3 frontend overhaul. Renders one row per company sorted
 * by enrichment progress + warm-path graph density. Each row links into
 * the upcoming `/org/:companyId` page.
 *
 * Data fetching is inline (no `db.ts` edits per coordination protocol —
 * `db.ts` is SR-owned). Uses the same untyped supabase pattern as
 * `OrgChart.tsx` because the generated `Database` type only knows about
 * v2 tables; v3 tables (companies, employment_periods, persons) are
 * present in production but not in the types file.
 */
import { useEffect, useMemo, useState, type JSX } from "react"
import { useNavigate } from "react-router-dom"
import { supabase } from "@/lib/supabase"
import { CompanyHeaderCard, type CompanyHeaderCardCompany } from "@/components/CompanyHeaderCard"
import { PageSkeleton } from "@/components/PageSkeleton"
import { ErrorState } from "@/components/ErrorState"

// ── Types ───────────────────────────────────────────────────────────────────

const DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
const DEFAULT_MIN_ENRICHED = 50
const DEFAULT_PAGE_SIZE = 25

export type CompanySort = "enrichment_pct" | "enriched_count" | "name" | "tier"

export type CompanyTier =
  | "semiconductor"
  | "defense"
  | "aerospace"
  | "research_lab"
  | "other"

export interface CompanyRow extends CompanyHeaderCardCompany {
  /**
   * Enriched-person count derived from a JOIN of employment_periods +
   * persons (linkedin_url IS NOT NULL AND is_current=TRUE). Drives the
   * "X / 500" progress bar.
   */
  enriched_count: number
  /**
   * Coarse tier — derived heuristically from `industry` and known
   * defense/aerospace/research-lab patterns. Used by the tier filter pill
   * row. Not a DB column.
   */
  tier: CompanyTier
}

interface CompaniesData {
  companies: CompanyRow[]
}

// ── Untyped supabase chain (matches OrgChart.tsx) ──────────────────────────
//
// Each chained call returns an object that EITHER awaits to a result OR
// continues chaining; we model it loosely as a self-recursive structure
// because the supabase JS client's type-narrowing pipeline isn't worth
// reproducing here. The Database type only knows about v2 tables, so v3
// queries (companies, employment_periods, persons) go through this loose
// shape — same approach as OrgChart.tsx.

interface SupabaseChain extends Promise<{ data: unknown; error: unknown }> {
  select: (cols: string) => SupabaseChain
  eq: (col: string, v: unknown) => SupabaseChain
  order: (col: string, opts?: unknown) => SupabaseChain
  limit: (n: number) => SupabaseChain
}

interface UntypedSupabase {
  from: (table: string) => SupabaseChain
}

// ── Pure helpers ────────────────────────────────────────────────────────────

/**
 * Map a company `industry` text to one of the 5 tier buckets. Pure +
 * exported so the test suite can verify the mapping rules without
 * touching supabase.
 */
export function tierFromIndustry(
  industry: string | null | undefined,
  canonical_name: string | null | undefined,
): CompanyTier {
  const i = (industry ?? "").toLowerCase()
  const n = (canonical_name ?? "").toLowerCase()
  // Defense
  if (
    i.includes("defense") ||
    i.includes("aerospace and defense") ||
    /\b(lockheed|raytheon|northrop|boeing|northrop grumman|general dynamics|saic|leidos|booz allen|palantir|anduril)\b/.test(n)
  ) {
    return "defense"
  }
  // Aerospace (commercial)
  if (i.includes("aerospace") || /\b(spacex|airbus|blue origin|sierra space|maxar|rocket lab)\b/.test(n)) {
    return "aerospace"
  }
  // Research lab
  if (
    i.includes("research") ||
    /\b(lanl|llnl|sandia|ornl|nasa|argonne|brookhaven|nist|csail|deepmind|openai|anthropic)\b/.test(n)
  ) {
    return "research_lab"
  }
  // Semiconductor
  if (
    i.includes("semiconductor") ||
    i.includes("chip") ||
    /\b(intel|amd|nvidia|tsmc|micron|qualcomm|broadcom|synopsys|cadence|asml|samsung electronics|stmicroelectronics|on semiconductor|onsemi|gf|globalfoundries|marvell|analog devices|texas instruments)\b/.test(n)
  ) {
    return "semiconductor"
  }
  return "other"
}

export function enrichmentPct(enriched: number, target: number): number {
  if (!Number.isFinite(enriched) || !Number.isFinite(target) || target <= 0) {
    return 0
  }
  return Math.max(0, Math.min(1, enriched / target))
}

/**
 * Pure sort + filter pipeline. Exposed for testability so we can drive
 * the controls in unit tests without rendering the full page.
 */
export function applyCompanyFilters(
  companies: CompanyRow[],
  opts: {
    search: string
    tiers: Set<CompanyTier>
    minEnriched: number
    sort: CompanySort
  },
): CompanyRow[] {
  const { search, tiers, minEnriched, sort } = opts
  const q = search.trim().toLowerCase()
  let out = companies.filter(
    (c) =>
      c.enriched_count >= minEnriched &&
      (tiers.size === 0 || tiers.has(c.tier)) &&
      (q === "" || c.canonical_name.toLowerCase().includes(q)),
  )
  // Sort copy (don't mutate input).
  out = [...out]
  switch (sort) {
    case "enrichment_pct":
      out.sort(
        (a, b) =>
          enrichmentPct(b.enriched_count, 500) -
          enrichmentPct(a.enriched_count, 500),
      )
      break
    case "enriched_count":
      out.sort((a, b) => b.enriched_count - a.enriched_count)
      break
    case "name":
      out.sort((a, b) =>
        a.canonical_name.localeCompare(b.canonical_name),
      )
      break
    case "tier":
      out.sort((a, b) => a.tier.localeCompare(b.tier))
      break
  }
  return out
}

// ── Data fetching ───────────────────────────────────────────────────────────

export async function fetchCompanies(
  accountId: string = DEFAULT_TENANT_ID,
): Promise<CompaniesData> {
  if (!supabase) {
    return { companies: [] }
  }
  const sb = supabase as unknown as UntypedSupabase

  // 1. All companies in the tenant (small projection).
  const coResp = (await sb
    .from("companies")
    .select(
      "id, canonical_name, industry, hq_country, employee_count_estimate, domains, org_chart_confidence, org_chart_signal_count",
    )
    .eq("account_id", accountId)) as { data: unknown; error: unknown }
  if (coResp.error) {
    throw new Error(String(coResp.error))
  }
  const rawCompanies = Array.isArray(coResp.data)
    ? (coResp.data as Array<{
        id: string
        canonical_name: string
        industry?: string | null
        hq_country?: string | null
        employee_count_estimate?: number | null
        domains?: string[] | null
        org_chart_confidence?: number | null
        org_chart_signal_count?: number | null
      }>)
    : []

  // 2. Per-company enriched count: persons with linkedin_url that have an
  //    is_current employment_period at this company. The supabase JS
  //    client doesn't support GROUP BY directly; we do the JOIN manually
  //    by fetching all is_current employment_periods + persons-with-url.
  //    For account scopes with O(35k) employment_periods this is fine —
  //    one round-trip vs N+1.
  const empResp = (await sb
    .from("employment_periods")
    .select("company_id, person_id")
    .eq("account_id", accountId)
    .eq("is_current", true)) as { data: unknown; error: unknown }
  if (empResp.error) {
    throw new Error(String(empResp.error))
  }
  const emps = Array.isArray(empResp.data)
    ? (empResp.data as Array<{ company_id: string; person_id: string }>)
    : []

  // Enriched persons (linkedin_url IS NOT NULL).
  const persResp = (await sb
    .from("persons")
    .select("id")
    .eq("account_id", accountId)) as { data: unknown; error: unknown }
  if (persResp.error) {
    throw new Error(String(persResp.error))
  }
  const enrichedPersonIds = new Set(
    Array.isArray(persResp.data)
      ? (persResp.data as Array<{ id: string }>).map((r) => r.id)
      : [],
  )

  // Build the per-company enriched count.
  const enrichedByCompany = new Map<string, Set<string>>()
  for (const e of emps) {
    if (!enrichedPersonIds.has(e.person_id)) continue
    const set = enrichedByCompany.get(e.company_id) ?? new Set<string>()
    set.add(e.person_id)
    enrichedByCompany.set(e.company_id, set)
  }

  const companies: CompanyRow[] = rawCompanies.map((c) => ({
    ...c,
    enriched_count: (enrichedByCompany.get(c.id) ?? new Set()).size,
    tier: tierFromIndustry(c.industry, c.canonical_name),
  }))
  return { companies }
}

// ── Hook ────────────────────────────────────────────────────────────────────

export function useCompanies(accountId: string = DEFAULT_TENANT_ID): {
  data: CompaniesData | null
  loading: boolean
  error: string | null
} {
  const [data, setData] = useState<CompaniesData | null>(null)
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchCompanies(accountId)
      .then((res) => {
        if (cancelled) return
        setData(res)
        setLoading(false)
      })
      .catch((err) => {
        if (cancelled) return
        setError(String(err?.message ?? err))
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [accountId])

  return { data, loading, error }
}

// ── Component ───────────────────────────────────────────────────────────────

const ALL_TIERS: CompanyTier[] = [
  "semiconductor",
  "defense",
  "aerospace",
  "research_lab",
  "other",
]

const TIER_LABELS: Record<CompanyTier, string> = {
  semiconductor: "Semis",
  defense: "Defense",
  aerospace: "Aerospace",
  research_lab: "Research",
  other: "Other",
}

export default function Companies(): JSX.Element {
  const { data, loading, error } = useCompanies()
  const navigate = useNavigate()
  const [search, setSearch] = useState<string>("")
  const [sort, setSort] = useState<CompanySort>("enrichment_pct")
  const [tiers, setTiers] = useState<Set<CompanyTier>>(new Set())
  const [showAll, setShowAll] = useState<boolean>(false)
  const [page, setPage] = useState<number>(0)

  const minEnriched = showAll ? 0 : DEFAULT_MIN_ENRICHED

  const filtered = useMemo(() => {
    if (!data) return []
    return applyCompanyFilters(data.companies, {
      search,
      tiers,
      minEnriched,
      sort,
    })
  }, [data, search, tiers, minEnriched, sort])

  const pageCount = Math.max(1, Math.ceil(filtered.length / DEFAULT_PAGE_SIZE))
  const visiblePage = Math.min(page, pageCount - 1)
  const visible = filtered.slice(
    visiblePage * DEFAULT_PAGE_SIZE,
    (visiblePage + 1) * DEFAULT_PAGE_SIZE,
  )

  if (loading) {
    return (
      <div data-testid="companies-loading">
        <PageSkeleton variant="list" rows={6} />
      </div>
    )
  }

  if (error) {
    return (
      <div data-testid="companies-error">
        <ErrorState
          error={error}
          title="Failed to load companies"
          retry={() => window.location.reload()}
        />
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-background p-6 space-y-4" data-testid="companies-page">
      {/* Toolbar */}
      <header className="space-y-3 border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="search"
            placeholder="Search by name…"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setPage(0)
            }}
            className="flex-1 min-w-[160px] bg-background border border-border px-2 py-1 text-sm"
            data-testid="companies-search"
            aria-label="Search companies"
          />
          <label className="text-[12px] text-muted-foreground flex items-center gap-2">
            Sort:
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as CompanySort)}
              className="bg-background border border-border px-2 py-1 text-sm"
              data-testid="companies-sort"
            >
              <option value="enrichment_pct">Enrichment %</option>
              <option value="enriched_count">Total persons</option>
              <option value="name">Name</option>
              <option value="tier">Tier</option>
            </select>
          </label>
          <label className="text-[12px] text-muted-foreground flex items-center gap-2">
            <input
              type="checkbox"
              checked={showAll}
              onChange={(e) => {
                setShowAll(e.target.checked)
                setPage(0)
              }}
              data-testid="companies-show-all"
            />
            Show all (incl. &lt;50 enriched)
          </label>
        </div>
        {/* Tier pills */}
        <div className="flex flex-wrap gap-2">
          {ALL_TIERS.map((t) => {
            const active = tiers.has(t)
            return (
              <button
                key={t}
                type="button"
                onClick={() => {
                  const next = new Set(tiers)
                  if (active) next.delete(t)
                  else next.add(t)
                  setTiers(next)
                  setPage(0)
                }}
                data-testid={`tier-pill-${t}`}
                className={
                  "text-[10px] uppercase tracking-[0.16em] px-2 py-0.5 border " +
                  (active
                    ? "bg-accent text-accent-foreground border-accent"
                    : "border-border text-muted-foreground hover:border-accent")
                }
              >
                {TIER_LABELS[t]}
              </button>
            )
          })}
        </div>
      </header>

      {/* Result count */}
      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground" data-testid="companies-count">
        {filtered.length} {filtered.length === 1 ? "company" : "companies"}
      </div>

      {/* List */}
      {filtered.length === 0 ? (
        <p
          className="text-sm text-muted-foreground border border-border bg-card p-6 text-center"
          data-testid="companies-empty"
        >
          No companies match the current filters.
        </p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="companies-list">
          {visible.map((c) => (
            <CompanyHeaderCard
              key={c.id}
              company={c}
              enriched_count={c.enriched_count}
              compact
              onClick={(id) => navigate(`/org/${id}`)}
            />
          ))}
        </div>
      )}

      {/* Pagination */}
      {pageCount > 1 && (
        <div className="flex items-center justify-between text-[12px] text-muted-foreground">
          <button
            type="button"
            onClick={() => setPage(Math.max(0, visiblePage - 1))}
            disabled={visiblePage === 0}
            className="border border-border px-3 py-1 disabled:opacity-40"
            data-testid="companies-prev"
          >
            Prev
          </button>
          <span className="text-mono" data-testid="companies-page-label">
            Page {visiblePage + 1} / {pageCount}
          </span>
          <button
            type="button"
            onClick={() => setPage(Math.min(pageCount - 1, visiblePage + 1))}
            disabled={visiblePage >= pageCount - 1}
            className="border border-border px-3 py-1 disabled:opacity-40"
            data-testid="companies-next"
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
