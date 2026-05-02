/**
 * Tests for the People page.
 *
 * Mocking strategy mirrors Companies.test.tsx: react-router-dom navigate
 * stubbed, supabase chain mocked with a queue of canned responses.
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
  act,
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
      in: () => chain,
      order: () => chain,
      limit: () => chain,
      not: () => chain,
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

// Import after mocks.
import People, {
  applyPeopleFilters,
  type PersonRow,
} from "./People"

beforeEach(() => {
  cleanup()
  mockNavigate.mockClear()
  supabaseQueue.length = 0
})

// ── Pure helper tests ──────────────────────────────────────────────────────

const sample: PersonRow[] = [
  {
    id: "p1",
    canonical_name: "Alice Kim",
    current_title: "VP Engineering",
    current_company_id: "co-intel",
    current_seniority_score: 70,
    current_functional_domain: "hardware_engineering",
    email: "alice@x.com",
    headline: "Hardware veteran at Intel",
    country_code: "US",
  },
  {
    id: "p2",
    canonical_name: "Bob Smith",
    current_title: "Software Engineer",
    current_company_id: "co-nvidia",
    current_seniority_score: 35,
    current_functional_domain: "software_engineering",
    email: null,
    headline: null,
    country_code: "US",
  },
  {
    id: "p3",
    canonical_name: "Charlie Park",
    current_title: "CTO",
    current_company_id: "co-acme",
    current_seniority_score: 90,
    current_functional_domain: "general_management",
    email: "charlie@y.com",
    headline: null,
    country_code: "KR",
  },
]

describe("applyPeopleFilters", () => {
  it("substring search matches name", () => {
    const out = applyPeopleFilters(sample, {
      search: "alice",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: false,
      sort: "canonical_name",
    })
    expect(out.map((p) => p.id)).toEqual(["p1"])
  })

  it("substring search matches headline", () => {
    const out = applyPeopleFilters(sample, {
      search: "veteran",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: false,
      sort: "canonical_name",
    })
    expect(out.map((p) => p.id)).toEqual(["p1"])
  })

  it("functional domain filter narrows the list", () => {
    const out = applyPeopleFilters(sample, {
      search: "",
      domains: new Set(["software_engineering"]),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: false,
      sort: "canonical_name",
    })
    expect(out.map((p) => p.id)).toEqual(["p2"])
  })

  it("country filter narrows the list", () => {
    const out = applyPeopleFilters(sample, {
      search: "",
      domains: new Set(),
      countries: new Set(["KR"]),
      minSeniority: 0,
      requireEmail: false,
      sort: "canonical_name",
    })
    expect(out.map((p) => p.id)).toEqual(["p3"])
  })

  it("min seniority filter excludes lower-tier persons", () => {
    const out = applyPeopleFilters(sample, {
      search: "",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 60,
      requireEmail: false,
      sort: "seniority_score",
    })
    expect(out.map((p) => p.id)).toEqual(["p3", "p1"])
  })

  it("requireEmail excludes persons without an email", () => {
    const out = applyPeopleFilters(sample, {
      search: "",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: true,
      sort: "canonical_name",
    })
    expect(out.map((p) => p.id)).toEqual(["p1", "p3"])
  })

  it("sorts by seniority desc with nulls last", () => {
    const out = applyPeopleFilters(sample, {
      search: "",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: false,
      sort: "seniority_score",
    })
    expect(out.map((p) => p.id)).toEqual(["p3", "p1", "p2"])
  })

  it("sorts by name asc", () => {
    const out = applyPeopleFilters(sample, {
      search: "",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: false,
      sort: "canonical_name",
    })
    expect(out.map((p) => p.canonical_name)).toEqual([
      "Alice Kim",
      "Bob Smith",
      "Charlie Park",
    ])
  })

  it("does not mutate input", () => {
    const before = sample.map((p) => p.id)
    applyPeopleFilters(sample, {
      search: "",
      domains: new Set(),
      countries: new Set(),
      minSeniority: 0,
      requireEmail: false,
      sort: "seniority_score",
    })
    expect(sample.map((p) => p.id)).toEqual(before)
  })
})

// ── Component tests ────────────────────────────────────────────────────────

describe("People page rendering", () => {
  it("shows loading skeleton then renders rows", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    expect(screen.getByTestId("people-loading")).toBeInTheDocument()
    await waitFor(() => screen.getByTestId("people-page"))
    const rows = screen.getAllByTestId(/^person-row-/)
    expect(rows.length).toBe(sample.length)
  })

  it("clicking a row navigates to /prospect/:id", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-page"))
    fireEvent.click(screen.getByTestId("person-row-p2"))
    expect(mockNavigate).toHaveBeenCalledWith("/prospect/p2")
  })

  it("renders ErrorState when supabase query fails", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({
      table: "persons",
      data: null,
      error: new Error("rls denied"),
    })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-error"))
    expect(screen.getByText(/rls denied/i)).toBeInTheDocument()
  })

  it("domain pill click filters the visible list", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-page"))
    fireEvent.click(screen.getByTestId("domain-pill-software_engineering"))
    const rows = screen.getAllByTestId(/^person-row-/)
    expect(rows.length).toBe(1)
    expect(screen.getByText("Bob Smith")).toBeInTheDocument()
  })

  it('"Has email" checkbox excludes null-email rows', async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-page"))
    fireEvent.click(screen.getByTestId("people-require-email"))
    const rows = screen.getAllByTestId(/^person-row-/)
    expect(rows.length).toBe(2)
  })

  it("min seniority slider filters lower-tier persons", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-page"))
    fireEvent.change(screen.getByTestId("people-min-seniority"), {
      target: { value: "60" },
    })
    const rows = screen.getAllByTestId(/^person-row-/)
    expect(rows.length).toBe(2)
  })

  it("renders empty placeholder when filters exclude everyone", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-page"))
    fireEvent.change(screen.getByTestId("people-min-seniority"), {
      target: { value: "100" },
    })
    expect(screen.getByTestId("people-empty")).toBeInTheDocument()
  })

  it("count summary updates with filters", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    render(<People />)
    await waitFor(() => screen.getByTestId("people-page"))
    expect(screen.getByTestId("people-count").textContent).toMatch(/3 of 3/)
    fireEvent.click(screen.getByTestId("domain-pill-software_engineering"))
    expect(screen.getByTestId("people-count").textContent).toMatch(/1 of 3/)
  })

  it("debounced search filters after 300ms", async () => {
    supabaseQueue.length = 0
    supabaseQueue.push({ table: "persons", data: sample })
    vi.useFakeTimers()
    render(<People />)
    // The mocked supabase fetch resolves on the microtask queue, not the
    // fake timer queue, so flush microtasks then advance to render.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    fireEvent.change(screen.getByTestId("people-search"), {
      target: { value: "alice" },
    })
    // Before debounce fires, all 3 rows still visible.
    expect(screen.getAllByTestId(/^person-row-/).length).toBe(3)
    await act(async () => {
      vi.advanceTimersByTime(300)
    })
    expect(screen.getAllByTestId(/^person-row-/).length).toBe(1)
    vi.useRealTimers()
  })
})
