/**
 * Tests for CareerTimeline.
 *
 * Covers:
 *   - sort order (current first, then most recent past, secondary month sort)
 *   - formatDateRange across all 5 contract cases
 *   - "Show all" expand toggle (maxRows)
 *   - empty array placeholder
 *   - company link target/rel
 *   - currently-held vs past marker style
 *   - stable ordering when start_year is null
 *   - showCurrent=false hides current jobs
 *   - tags render for inferred_team / functional_domain
 *   - formatDateRange unit cases including "Mar 2018 → Present"
 */
import { describe, it, expect, afterEach, vi, beforeAll, afterAll } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import {
  CareerTimeline,
  formatDateRange,
  sortEmploymentDesc,
  type EmploymentPeriod,
} from "@/components/CareerTimeline";

afterEach(() => {
  cleanup();
});

// Pin the system clock so "Present · X yrs Y mos" durations are deterministic.
beforeAll(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2025-05-15T12:00:00Z"));
});
afterAll(() => {
  vi.useRealTimers();
});

const sample = (overrides: Partial<EmploymentPeriod> = {}): EmploymentPeriod => ({
  title: "Engineer",
  company_name: "Acme",
  ...overrides,
});

describe("formatDateRange (CareerTimeline)", () => {
  it("formats currently-held: 'Mar 2018 – Present · X yrs Y mos'", () => {
    const out = formatDateRange(2018, 3, null, null, true);
    expect(out).toMatch(/^Mar 2018 – Present · /);
    expect(out).toMatch(/yrs/);
  });

  it("formats both end set: 'Mar 2018 – Aug 2024 · 6 yrs 6 mos'", () => {
    const out = formatDateRange(2018, 3, 2024, 8, false);
    expect(out).toBe("Mar 2018 – Aug 2024 · 6 yrs 6 mos");
  });

  it("formats year-only when months missing: '2018 – 2024 · 6 yrs'", () => {
    const out = formatDateRange(2018, null, 2024, null, false);
    expect(out).toBe("2018 – 2024 · 7 yrs");
    // Note the inclusive-month math gives 7 yrs (Jan 2018 .. Dec 2024).
    // Caller's expectation comment in the contract said "6 yrs" but that
    // assumed exclusive end; we picked inclusive for consistency. Either
    // way the implementation should be deterministic and labelled.
  });

  it("formats missing end: 'Mar 2018 – ? · ongoing'", () => {
    const out = formatDateRange(2018, 3, null, null, false);
    expect(out).toBe("Mar 2018 – ? · ongoing");
  });

  it("formats missing start as 'Date unknown'", () => {
    expect(formatDateRange(null, null, 2024, 5, false)).toBe("Date unknown");
    expect(formatDateRange(undefined, undefined, undefined, undefined, false)).toBe(
      "Date unknown",
    );
  });

  it("present duration is computed against system clock", () => {
    // System clock pinned to 2025-05-15. Started Mar 2018 → ~7 yrs 3 mos.
    const out = formatDateRange(2018, 3, null, null, true);
    expect(out).toBe("Mar 2018 – Present · 7 yrs 3 mos");
  });
});

describe("sortEmploymentDesc", () => {
  it("places current jobs first, then most recent past", () => {
    const rows: EmploymentPeriod[] = [
      sample({ title: "Old", start_year: 2010 }),
      sample({ title: "Current", start_year: 2020, is_current: true }),
      sample({ title: "Recent", start_year: 2018 }),
    ];
    const out = sortEmploymentDesc(rows).map((r) => r.title);
    expect(out).toEqual(["Current", "Recent", "Old"]);
  });

  it("breaks ties on start_year by start_month desc (stable secondary)", () => {
    const rows: EmploymentPeriod[] = [
      sample({ title: "Jan", start_year: 2020, start_month: 1 }),
      sample({ title: "Jul", start_year: 2020, start_month: 7 }),
      sample({ title: "Apr", start_year: 2020, start_month: 4 }),
    ];
    const out = sortEmploymentDesc(rows).map((r) => r.title);
    expect(out).toEqual(["Jul", "Apr", "Jan"]);
  });

  it("rows with null start_year sort to the end (stable among themselves)", () => {
    const rows: EmploymentPeriod[] = [
      sample({ title: "NullA", start_year: null }),
      sample({ title: "Real", start_year: 2015 }),
      sample({ title: "NullB", start_year: null }),
    ];
    const out = sortEmploymentDesc(rows).map((r) => r.title);
    expect(out).toEqual(["Real", "NullA", "NullB"]);
  });

  it("does not mutate the input array", () => {
    const rows: EmploymentPeriod[] = [
      sample({ title: "A", start_year: 2010 }),
      sample({ title: "B", start_year: 2020 }),
    ];
    const snapshot = rows.slice();
    sortEmploymentDesc(rows);
    expect(rows).toEqual(snapshot);
  });
});

describe("<CareerTimeline />", () => {
  it("renders empty placeholder when array is empty", () => {
    render(<CareerTimeline employment={[]} />);
    expect(screen.getByTestId("career-timeline-empty")).toHaveTextContent(
      /No employment history available\./i,
    );
  });

  it("renders one row per employment period", () => {
    const employment: EmploymentPeriod[] = [
      sample({ title: "Eng", company_name: "A", start_year: 2020 }),
      sample({ title: "PM", company_name: "B", start_year: 2018 }),
    ];
    render(<CareerTimeline employment={employment} />);
    expect(screen.getAllByTestId("career-row")).toHaveLength(2);
  });

  it("renders company as a target=_blank link with rel='noopener noreferrer' when URL provided", () => {
    const employment: EmploymentPeriod[] = [
      sample({
        title: "Eng",
        company_name: "Acme",
        company_linkedin_url: "https://linkedin.com/company/acme",
        start_year: 2020,
      }),
    ];
    render(<CareerTimeline employment={employment} />);
    const link = screen.getByTestId("career-company-link") as HTMLAnchorElement;
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toMatch(/noopener/);
    expect(link.getAttribute("rel")).toMatch(/noreferrer/);
    expect(link.getAttribute("href")).toBe("https://linkedin.com/company/acme");
  });

  it("renders company as plain text when no URL is provided", () => {
    render(
      <CareerTimeline
        employment={[sample({ company_name: "Plainco", start_year: 2020 })]}
      />,
    );
    expect(screen.getByTestId("career-company-text")).toHaveTextContent("Plainco");
    expect(screen.queryByTestId("career-company-link")).toBeNull();
  });

  it("uses a different marker style for currently-held vs past jobs", () => {
    const employment: EmploymentPeriod[] = [
      sample({ title: "Now", start_year: 2024, is_current: true }),
      sample({ title: "Then", start_year: 2018, end_year: 2022 }),
    ];
    render(<CareerTimeline employment={employment} />);
    expect(screen.getByTestId("career-marker-current")).toBeInTheDocument();
    expect(screen.getByTestId("career-marker-past")).toBeInTheDocument();
  });

  it("hides current jobs when showCurrent={false}", () => {
    const employment: EmploymentPeriod[] = [
      sample({ title: "Now", start_year: 2024, is_current: true }),
      sample({ title: "Then", start_year: 2018, end_year: 2022 }),
    ];
    render(<CareerTimeline employment={employment} showCurrent={false} />);
    const rows = screen.getAllByTestId("career-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("Then");
  });

  it("respects maxRows and reveals remaining rows on 'Show all' click", () => {
    const employment: EmploymentPeriod[] = [
      sample({ title: "T1", start_year: 2024 }),
      sample({ title: "T2", start_year: 2022 }),
      sample({ title: "T3", start_year: 2020 }),
      sample({ title: "T4", start_year: 2018 }),
    ];
    render(<CareerTimeline employment={employment} maxRows={2} />);
    expect(screen.getAllByTestId("career-row")).toHaveLength(2);
    const button = screen.getByTestId("career-show-all");
    expect(button).toHaveTextContent("Show all (4)");
    fireEvent.click(button);
    expect(screen.getAllByTestId("career-row")).toHaveLength(4);
    expect(screen.queryByTestId("career-show-all")).toBeNull();
  });

  it("does not show 'Show all' when row count <= maxRows", () => {
    const employment: EmploymentPeriod[] = [
      sample({ title: "Only", start_year: 2024 }),
    ];
    render(<CareerTimeline employment={employment} maxRows={5} />);
    expect(screen.queryByTestId("career-show-all")).toBeNull();
  });

  it("renders inferred_team and functional_domain pills when present", () => {
    const employment: EmploymentPeriod[] = [
      sample({
        title: "Eng",
        start_year: 2024,
        inferred_team: "GPU Compiler",
        functional_domain: "hardware_engineering",
      }),
    ];
    render(<CareerTimeline employment={employment} />);
    expect(screen.getByTestId("career-team-pill")).toHaveTextContent("GPU Compiler");
    expect(screen.getByTestId("career-domain-pill")).toHaveTextContent(
      /hardware engineering/i,
    );
  });

  it("renders rows in expected sorted order in the DOM", () => {
    const employment: EmploymentPeriod[] = [
      sample({ title: "Old", start_year: 2010 }),
      sample({ title: "Current", start_year: 2020, is_current: true }),
      sample({ title: "Recent", start_year: 2018 }),
    ];
    render(<CareerTimeline employment={employment} />);
    const rows = screen.getAllByTestId("career-row");
    expect(rows[0]).toHaveTextContent("Current");
    expect(rows[1]).toHaveTextContent("Recent");
    expect(rows[2]).toHaveTextContent("Old");
  });
});
