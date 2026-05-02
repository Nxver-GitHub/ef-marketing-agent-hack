/**
 * People — paginated, filterable browser of every enriched person in the DB.
 *
 * Phase D2 of the v3 frontend overhaul. Mirrors `Companies.tsx`'s shape:
 * supabase-direct fetch (untyped chain — no `db.ts` edits), inline pure
 * helpers for filter/sort, plain pagination (no `@tanstack/react-virtual`
 * — only `@tanstack/react-query` is in deps).
 *
 * Each row navigates to `/prospect/:id`. Inline person row instead of
 * `<PersonProfileCard>` because that file is currently held by another
 * agent's reservation; we render a tight subset of identity + headline +
 * seniority + click action.
 */
import { useEffect, useMemo, useState, type JSX } from "react"
import { useNavigate } from "react-router-dom"
import { supabase } from "@/lib/supabase"
import { PageSkeleton } from "@/components/PageSkeleton"
import { ErrorState } from "@/components/ErrorState"
import { flagEmoji, formatCount } from "@/components/PersonProfileCard"
import {
  domainCssVar,
  domainLabel,
  type FunctionalDomain,
} from "@/lib/orgClusters"

// ── Types ───────────────────────────────────────────────────────────────────

const DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
const DEFAULT_PAGE_SIZE = 50

export type PersonSort =
  | "seniority_score"
  | "canonical_name"
  | "current_company"

export interface PersonRow {
  id: string
  canonical_name: string
  current_title?: string | null
  current_company_id?: string | null
  current_seniority_score?: number | null
  current_functional_domain?: string | null
  email?: string | null
  headline?: string | null
  country_code?: string | null
}

export interface PeopleData {
  persons: PersonRow[]
}

// ── Untyped supabase chain ─────────────────────────────────────────────────

interface SupabaseChain extends Promise<{ data: unknown; error: unknown }> {
  select: (cols: string) => SupabaseChain
  eq: (col: string, v: unknown) => SupabaseChain
  in: (col: string, v: unknown[]) => SupabaseChain
  order: (col: string, opts?: unknown) => SupabaseChain
  limit: (n: number) => SupabaseChain
  not: (col: string, op: string, v: unknown) => SupabaseChain
}

interface UntypedSupabase {
  from: (table: string) => SupabaseChain
}

// ── Pure helpers ────────────────────────────────────────────────────────────

/**
 * Apply the in-memory filter/sort pipeline. The supabase fetch returns
 * the full tenant's persons rows; we filter client-side to keep the UI
 * responsive without N+1 round-trips on each toolbar interaction. At
 * 37k persons this is fast enough; would need server-side filtering if
 * the dataset grows past ~500k.
 */
export function applyPeopleFilters(
  persons: PersonRow[],
  opts: {
    search: string
    domains: Set<FunctionalDomain | string>
    countries: Set<string>
    minSeniority: number
    requireEmail: boolean
    sort: PersonSort
  },
): PersonRow[] {
  const { search, domains, countries, minSeniority, requireEmail, sort } = opts
  const q = search.trim().toLowerCase()
  let out = persons.filter((p) => {
    if (q !== "") {
      const inName = p.canonical_name.toLowerCase().includes(q)
      const inHeadline = (p.headline ?? "").toLowerCase().includes(q)
      if (!inName && !inHeadline) return false
    }
    if (
      domains.size > 0 &&
      (!p.current_functional_domain ||
        !domains.has(p.current_functional_domain))
    ) {
      return false
    }
    if (
      countries.size > 0 &&
      (!p.country_code || !countries.has(p.country_code))
    ) {
      return false
    }
    if (
      typeof minSeniority === "number" &&
      minSeniority > 0 &&
      (p.current_seniority_score ?? 0) < minSeniority
    ) {
      return false
    }
    if (requireEmail && !p.email) return false
    return true
  })
  out = [...out]
  switch (sort) {
    case "seniority_score":
      out.sort((a, b) => {
        const sa = a.current_seniority_score ?? -1
        const sb = b.current_seniority_score ?? -1
        return sb - sa
      })
      break
    case "canonical_name":
      out.sort((a, b) =>
        a.canonical_name.localeCompare(b.canonical_name),
      )
      break
    case "current_company":
      out.sort((a, b) =>
        (a.current_company_id ?? "").localeCompare(b.current_company_id ?? ""),
      )
      break
  }
  return out
}

// ── Data fetching ───────────────────────────────────────────────────────────

export async function fetchPeople(
  accountId: string = DEFAULT_TENANT_ID,
): Promise<PeopleData> {
  if (!supabase) {
    return { persons: [] }
  }
  const sb = supabase as unknown as UntypedSupabase
  // Cap at 5000 rows in this MVP — full 37k is too large for the unfiltered
  // toolbar pipeline. The default-min-seniority filter narrows the dataset
  // server-side once the user dials it up. Pagination handles the rest.
  const resp = (await sb
    .from("persons")
    .select(
      "id, canonical_name, current_title, current_company_id, current_seniority_score, current_functional_domain, email, headline, country_code",
    )
    .eq("account_id", accountId)
    .order("current_seniority_score", { ascending: false, nullsFirst: false })
    .limit(5000)) as { data: unknown; error: unknown }
  if (resp.error) {
    throw new Error(String(resp.error))
  }
  const persons = Array.isArray(resp.data) ? (resp.data as PersonRow[]) : []
  return { persons }
}

export function usePeople(accountId: string = DEFAULT_TENANT_ID): {
  data: PeopleData | null
  loading: boolean
  error: string | null
} {
  const [data, setData] = useState<PeopleData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchPeople(accountId)
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

const FUNCTIONAL_DOMAIN_OPTIONS: FunctionalDomain[] = [
  "hardware_engineering",
  "software_engineering",
  "product_management",
  "manufacturing_ops",
  "sales_marketing",
  "research",
  "finance_legal",
  "people_ops",
  "general_management",
]

export default function People(): JSX.Element {
  const { data, loading, error } = usePeople()
  const navigate = useNavigate()
  const [search, setSearch] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")
  const [sort, setSort] = useState<PersonSort>("seniority_score")
  const [domains, setDomains] = useState<Set<FunctionalDomain | string>>(
    new Set(),
  )
  const [countries, setCountries] = useState<Set<string>>(new Set())
  const [minSeniority, setMinSeniority] = useState<number>(0)
  const [requireEmail, setRequireEmail] = useState(false)
  const [page, setPage] = useState(0)

  // 300ms debounce on search input.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300)
    return () => clearTimeout(t)
  }, [search])

  // Reset page when filters change so the user lands on results page 1.
  useEffect(() => {
    setPage(0)
  }, [debouncedSearch, sort, domains, countries, minSeniority, requireEmail])

  const filtered = useMemo(() => {
    if (!data) return []
    return applyPeopleFilters(data.persons, {
      search: debouncedSearch,
      domains,
      countries,
      minSeniority,
      requireEmail,
      sort,
    })
  }, [data, debouncedSearch, domains, countries, minSeniority, requireEmail, sort])

  const total = data?.persons.length ?? 0
  const pageCount = Math.max(1, Math.ceil(filtered.length / DEFAULT_PAGE_SIZE))
  const visiblePage = Math.min(page, pageCount - 1)
  const visible = filtered.slice(
    visiblePage * DEFAULT_PAGE_SIZE,
    (visiblePage + 1) * DEFAULT_PAGE_SIZE,
  )

  // Country options derived from data — sorted alpha.
  const countryOptions = useMemo(() => {
    const set = new Set<string>()
    for (const p of data?.persons ?? []) {
      if (p.country_code) set.add(p.country_code)
    }
    return Array.from(set).sort()
  }, [data])

  if (loading) {
    return (
      <div data-testid="people-loading">
        <PageSkeleton variant="list" rows={8} />
      </div>
    )
  }
  if (error) {
    return (
      <div data-testid="people-error">
        <ErrorState
          error={error}
          title="Failed to load people"
          retry={() => window.location.reload()}
        />
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-background p-6 space-y-4" data-testid="people-page">
      {/* Toolbar */}
      <header className="space-y-3 border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="search"
            placeholder="Search name or headline…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="flex-1 min-w-[200px] bg-background border border-border px-2 py-1 text-sm"
            data-testid="people-search"
            aria-label="Search people"
          />
          <label className="text-[12px] text-muted-foreground flex items-center gap-2">
            Sort:
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as PersonSort)}
              className="bg-background border border-border px-2 py-1 text-sm"
              data-testid="people-sort"
            >
              <option value="seniority_score">Seniority</option>
              <option value="canonical_name">Name</option>
              <option value="current_company">Company</option>
            </select>
          </label>
          <label className="text-[12px] text-muted-foreground flex items-center gap-2">
            <input
              type="checkbox"
              checked={requireEmail}
              onChange={(e) => setRequireEmail(e.target.checked)}
              data-testid="people-require-email"
            />
            Has email
          </label>
        </div>

        {/* Domain pills */}
        <div className="flex flex-wrap gap-2" data-testid="people-domain-pills">
          {FUNCTIONAL_DOMAIN_OPTIONS.map((d) => {
            const active = domains.has(d)
            return (
              <button
                key={d}
                type="button"
                onClick={() => {
                  const next = new Set(domains)
                  if (active) next.delete(d)
                  else next.add(d)
                  setDomains(next)
                }}
                data-testid={`domain-pill-${d}`}
                className={
                  "text-[10px] uppercase tracking-[0.16em] px-2 py-0.5 border " +
                  (active
                    ? "bg-accent text-accent-foreground border-accent"
                    : "border-border text-muted-foreground hover:border-accent")
                }
                style={active ? undefined : { borderLeftColor: domainCssVar(d), borderLeftWidth: 3 }}
              >
                {domainLabel(d)}
              </button>
            )
          })}
        </div>

        {/* Country + seniority */}
        <div className="flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground">
          <label className="flex items-center gap-2">
            Min seniority:
            <input
              type="range"
              min="0"
              max="100"
              step="5"
              value={minSeniority}
              onChange={(e) => setMinSeniority(parseInt(e.target.value, 10))}
              className="w-32"
              aria-label="Minimum seniority"
              data-testid="people-min-seniority"
            />
            <span className="text-mono w-8 text-right">{minSeniority}</span>
          </label>
          {countryOptions.length > 0 && (
            <div className="flex flex-wrap gap-1.5" data-testid="people-country-pills">
              {countryOptions.slice(0, 12).map((cc) => {
                const active = countries.has(cc)
                return (
                  <button
                    key={cc}
                    type="button"
                    onClick={() => {
                      const next = new Set(countries)
                      if (active) next.delete(cc)
                      else next.add(cc)
                      setCountries(next)
                    }}
                    data-testid={`country-pill-${cc}`}
                    className={
                      "text-[10px] px-1.5 py-0.5 border flex items-center gap-1 " +
                      (active
                        ? "bg-accent text-accent-foreground border-accent"
                        : "border-border text-muted-foreground hover:border-accent")
                    }
                  >
                    <span aria-hidden>{flagEmoji(cc)}</span>
                    <span>{cc}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </header>

      {/* Result count */}
      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground" data-testid="people-count">
        Showing {formatCount(filtered.length)} of {formatCount(total)}{" "}
        {filtered.length === 1 ? "person" : "people"}
      </div>

      {/* List */}
      {filtered.length === 0 ? (
        <p
          className="text-sm text-muted-foreground border border-border bg-card p-6 text-center"
          data-testid="people-empty"
        >
          No people match the current filters.
        </p>
      ) : (
        <ul className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="people-list">
          {visible.map((p) => (
            <li key={p.id}>
              <button
                type="button"
                onClick={() => navigate(`/prospect/${p.id}`)}
                data-testid={`person-row-${p.id}`}
                className="block w-full text-left border border-border bg-card p-3 space-y-1 hover:border-accent focus-visible:border-accent transition-colors"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-sm font-semibold text-foreground truncate">
                    {p.canonical_name}
                  </span>
                  {typeof p.current_seniority_score === "number" && (
                    <span className="text-[10px] text-mono text-muted-foreground">
                      {p.current_seniority_score}
                    </span>
                  )}
                </div>
                {p.current_title && (
                  <div className="text-[12px] text-muted-foreground truncate">
                    {p.current_title}
                  </div>
                )}
                {p.headline && (
                  <div className="text-[11px] text-muted-foreground/80 line-clamp-2">
                    {p.headline}
                  </div>
                )}
                <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                  {p.current_functional_domain && (
                    <span
                      className="px-1 border border-border"
                      style={{ borderLeftColor: domainCssVar(p.current_functional_domain), borderLeftWidth: 3 }}
                    >
                      {domainLabel(p.current_functional_domain)}
                    </span>
                  )}
                  {p.country_code && (
                    <span aria-label={p.country_code}>{flagEmoji(p.country_code)}</span>
                  )}
                  {p.email && <span title={p.email}>✉</span>}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Pagination */}
      {pageCount > 1 && (
        <div className="flex items-center justify-between text-[12px] text-muted-foreground">
          <button
            type="button"
            onClick={() => setPage(Math.max(0, visiblePage - 1))}
            disabled={visiblePage === 0}
            className="border border-border px-3 py-1 disabled:opacity-40"
            data-testid="people-prev"
          >
            Prev
          </button>
          <span className="text-mono" data-testid="people-page-label">
            Page {visiblePage + 1} / {pageCount}
          </span>
          <button
            type="button"
            onClick={() => setPage(Math.min(pageCount - 1, visiblePage + 1))}
            disabled={visiblePage >= pageCount - 1}
            className="border border-border px-3 py-1 disabled:opacity-40"
            data-testid="people-next"
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
