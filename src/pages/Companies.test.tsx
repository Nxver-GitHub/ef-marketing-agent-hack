/**
 * Tests for the Companies page (/companies).
 *
 * Mocking strategy:
 *   - react-router-dom: useNavigate stubbed.
 *   - @/lib/supabase: chainable mock with a queue of canned responses.
 *
 * Pure-helper tests run against the exported `tierFromIndustry`,
 * `enrichmentPct`, `applyCompanyFilters` so we get coverage of the
 * sort/filter/search rules without rendering the component.
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
} from "@testing-library/react"

const mockNavigate = vi.fn()
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}))

// ── supabase mock ──────────────────────────────────────────────────────────

interface SupabaseQueueEntry {
  table: string
  data: unknown
  error?: unknown
}
const supabaseQueue: SupabaseQueueEntry[] = []

vi.mock("@/lib/supabase", () => {
  // Builder lazy-resolves to the queued entry on first await, so chained
  // .select().eq().eq() / .eq().order().limit() all return the same chain
  // object. The Promise resolution is shared across all chain methods.
  const makeBuilder = (table: string) => {
    let resolved: Promise<{ data: unknown; error: unknown }> | null = null
    const resolve = () => {
      if (resolved) return resolved
      const idx = supabaseQueue.findIndex((q) => q.table === table)
      if (idx === -1) {
        resolved = Promise.resolve({ data: [], error: null })
      } else {
        const entry = supabaseQueue.splice(idx, 1)[0]
        resolved = Promise.resolve({
          data: entry.data,
          error: entry.error ?? null,
        })
      }
      return resolved
    }
    const chain: Record<string, unknown> = {
      select: () => chain,
      eq: () => chain,
      order: () => chain,
      limit: () => chain,
      then: (
        ok?: (value: { data: unknown; error: unknown }) => unknown,
        err?: (reason: unknown) => unknown,
      ) => resolve().then(ok, err),
      catch: (err?: (reason: unknown) => unknown) => resolve().catch(err),
      finally: (cb?: () => void) => resolve().finally(cb),
    }
    return chain as unknown as PromiseLike<unknown> & typeof chain
  }
  return {
    supabase: {
      from: (table: string) => makeBuilder(table),
    },
    HAS_REAL_SUPABASE: true,
  }
})

// Import after mocks so the page picks them up.
import Companies, {
  tierFromIndustry,
  enrichmentPct,
  applyCompanyFilters,
  type CompanyRow,
  type CompanyTier,
} from "./Companies"

// ── Fixtures ────────────────────────────────────────────────────────────────

function row(
  id: string,
  overrides: Partial<CompanyRow> = {},
): CompanyRow {
  return {
    id,
    canonical_name: id.toUpperCase(),
    enriched_count: 100,
    tier: "semiconductor",
    ...overrides,
  }
}

function queueDefaultLoad(companies: Array<Partial<{
  id: string
  canonical_name: string
  industry: string
  hq_country: string
  domains: string[]
}>>) {
  supabaseQueue.length = 0
  supabaseQueue.push({ table: "companies", data: companies })
  // Empty employment_periods + persons → enriched_count=0 for everyone.
  supabaseQueue.push({ table: "employment_periods", data: [] })
  supabaseQueue.push({ table: "persons", data: [] })
}

beforeEach(() => {
  cleanup()
  mockNavigate.mockClear()
  supabaseQueue.length = 0
})

// ── Pure helpers ────────────────────────────────────────────────────────────

describe("tierFromIndustry", () => {
  it("classifies semiconductor by industry text", () => {
    expect(tierFromIndustry("Semiconductors", null)).toBe("semiconductor")
    expect(tierFromIndustry("semiconductor design", null)).toBe("semiconductor")
  })

  it("classifies semiconductor by canonical name", () => {
    expect(tierFromIndustry(null, "Intel")).toBe("semiconductor")
    expect(tierFromIndustry(null, "NVIDIA Corporation")).toBe("semiconductor")
    expect(tierFromIndustry(null, "TSMC")).toBe("semiconductor")
  })

  it("classifies defense by industry or name", () => {
    expect(tierFromIndustry("Defense", null)).toBe("defense")
    expect(tierFromIndustry(null, "Lockheed Martin")).toBe("defense")
    expect(tierFromIndustry(null, "Anduril Industries")).toBe("defense")
  })

  it("classifies aerospace, research_lab, other", () => {
    expect(tierFromIndustry("Aerospace", null)).toBe("aerospace")
    expect(tierFromIndustry(null, "SpaceX")).toBe("aerospace")
    expect(tierFromIndustry(null, "LANL")).toBe("research_lab")
    expect(tierFromIndustry(null, "OpenAI")).toBe("research_lab")
    expect(tierFromIndustry("Bookstore", "Acme")).toBe("other")
  })

  it("handles null / empty input", () => {
    expect(tierFromIndustry(null, null)).toBe("other")
    expect(tierFromIndustry(undefined, undefined)).toBe("other")
    expect(tierFromIndustry("", "")).toBe("other")
  })
})

describe("enrichmentPct", () => {
  it("clamps to [0, 1]", () => {
    expect(enrichmentPct(250, 500)).toBe(0.5)
    expect(enrichmentPct(700, 500)).toBe(1)
    expect(enrichmentPct(-10, 500)).toBe(0)
  })

  it("returns 0 for invalid inputs", () => {
    expect(enrichmentPct(NaN, 500)).toBe(0)
    expect(enrichmentPct(100, 0)).toBe(0)
    expect(enrichmentPct(100, -1)).toBe(0)
  })
})

describe("applyCompanyFilters", () => {
  const fixtures: CompanyRow[] = [
    row("intel", { canonical_name: "Intel", enriched_count: 400, tier: "semiconductor" }),
    row("nvidia", { canonical_name: "NVIDIA", enriched_count: 250, tier: "semiconductor" }),
    row("lockheed", { canonical_name: "Lockheed", enriched_count: 100, tier: "defense" }),
    row("low", { canonical_name: "Tiny Co", enriched_count: 30, tier: "other" }),
  ]

  it("hides cos with enriched_count < minEnriched by default", () => {
    const out = applyCompanyFilters(fixtures, {
      search: "",
      tiers: new Set(),
      minEnriched: 50,
      sort: "enriched_count",
    })
    expect(out.map((c) => c.id)).toEqual(["intel", "nvidia", "lockheed"])
  })

  it("show all (minEnriched=0) reveals low-coverage cos", () => {
    const out = applyCompanyFilters(fixtures, {
      search: "",
      tiers: new Set(),
      minEnriched: 0,
      sort: "enriched_count",
    })
    expect(out.map((c) => c.id)).toEqual(["intel", "nvidia", "lockheed", "low"])
  })

  it("tier filter narrows the list", () => {
    const out = applyCompanyFilters(fixtures, {
      search: "",
      tiers: new Set<CompanyTier>(["defense"]),
      minEnriched: 0,
      sort: "name",
    })
    expect(out.map((c) => c.id)).toEqual(["lockheed"])
  })

  it("search substring-matches canonical_name (case-insensitive)", () => {
    const out = applyCompanyFilters(fixtures, {
      search: "INTEL",
      tiers: new Set(),
      minEnriched: 0,
      sort: "name",
    })
    expect(out.map((c) => c.id)).toEqual(["intel"])
  })

  it("sorts by enrichment_pct desc", () => {
    const out = applyCompanyFilters(fixtures, {
      search: "",
      tiers: new Set(),
      minEnriched: 0,
      sort: "enrichment_pct",
    })
    expect(out.map((c) => c.id)).toEqual(["intel", "nvidia", "lockheed", "low"])
  })

  it("sorts by name asc", () => {
    const out = applyCompanyFilters(fixtures, {
      search: "",
      tiers: new Set(),
      minEnriched: 0,
      sort: "name",
    })
    expect(out.map((c) => c.canonical_name)).toEqual([
      "Intel",
      "Lockheed",
      "NVIDIA",
      "Tiny Co",
    ])
  })

  it("does not mutate input", () => {
    const before = fixtures.map((c) => c.id)
    applyCompanyFilters(fixtures, {
      search: "",
      tiers: new Set(),
      minEnriched: 0,
      sort: "name",
    })
    expect(fixtures.map((c) => c.id)).toEqual(before)
  })
})

// ── Component (live data flow via mocked supabase) ─────────────────────────

describe("Companies page rendering", () => {
  it("renders one card per company after data loads", async () => {
    queueDefaultLoad([
      { id: "co-a", canonical_name: "Acme", industry: "Semiconductors" },
      { id: "co-b", canonical_name: "Beta", industry: "Semiconductors" },
    ])
    render(<Companies />)
    // Loading state up first.
    expect(screen.getByTestId("companies-loading")).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByTestId("companies-page")).toBeInTheDocument(),
    )
    // Default min-enriched filter (50) hides everyone (enriched_count=0).
    expect(screen.getByTestId("companies-empty")).toBeInTheDocument()
    // Toggle "show all" → both render.
    fireEvent.click(screen.getByTestId("companies-show-all"))
    const cards = screen.getAllByTestId("company-header-card")
    expect(cards.length).toBe(2)
  })

  it("clicking a card navigates to /org/:companyId", async () => {
    queueDefaultLoad([
      { id: "co-1", canonical_name: "First Co", industry: "Semiconductors" },
    ])
    render(<Companies />)
    await waitFor(() => screen.getByTestId("companies-page"))
    fireEvent.click(screen.getByTestId("companies-show-all"))
    const card = screen.getByTestId("company-header-card")
    fireEvent.click(card)
    expect(mockNavigate).toHaveBeenCalledWith("/org/co-1")
  })

  it("search input filters the visible cards", async () => {
    queueDefaultLoad([
      { id: "co-a", canonical_name: "Apple Inc" },
      { id: "co-b", canonical_name: "Banana Co" },
    ])
    render(<Companies />)
    await waitFor(() => screen.getByTestId("companies-page"))
    fireEvent.click(screen.getByTestId("companies-show-all"))
    fireEvent.change(screen.getByTestId("companies-search"), {
      target: { value: "apple" },
    })
    const cards = screen.getAllByTestId("company-header-card")
    expect(cards.length).toBe(1)
    expect(screen.getByText("Apple Inc")).toBeInTheDocument()
  })

  it("tier pill click narrows the result", async () => {
    queueDefaultLoad([
      { id: "co-a", canonical_name: "Intel", industry: "Semiconductors" },
      { id: "co-b", canonical_name: "Lockheed Martin", industry: "Defense" },
    ])
    render(<Companies />)
    await waitFor(() => screen.getByTestId("companies-page"))
    fireEvent.click(screen.getByTestId("companies-show-all"))
    fireEvent.click(screen.getByTestId("tier-pill-defense"))
    const cards = screen.getAllByTestId("company-header-card")
    expect(cards.length).toBe(1)
    expect(screen.getByText("Lockheed Martin")).toBeInTheDocument()
  })

  it("renders empty placeholder when no companies match", async () => {
    queueDefaultLoad([
      { id: "co-a", canonical_name: "Acme", industry: "Semiconductors" },
    ])
    render(<Companies />)
    await waitFor(() => screen.getByTestId("companies-page"))
    // Default minEnriched=50 hides everyone (no employment_periods queued).
    expect(screen.getByTestId("companies-empty")).toBeInTheDocument()
  })

  it("renders error state when supabase query fails", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({
      table: "companies",
      data: null,
      error: new Error("rls policy denied"),
    })
    render(<Companies />)
    await waitFor(() => screen.getByTestId("companies-error"))
    expect(screen.getByText(/rls policy denied/i)).toBeInTheDocument()
  })

  it("show count summary updates with filters", async () => {
    queueDefaultLoad([
      { id: "co-a", canonical_name: "Intel", industry: "Semiconductors" },
      { id: "co-b", canonical_name: "Lockheed", industry: "Defense" },
    ])
    render(<Companies />)
    await waitFor(() => screen.getByTestId("companies-page"))
    fireEvent.click(screen.getByTestId("companies-show-all"))
    expect(screen.getByTestId("companies-count").textContent).toContain("2")
    fireEvent.click(screen.getByTestId("tier-pill-semiconductor"))
    expect(screen.getByTestId("companies-count").textContent).toContain("1")
  })
})
