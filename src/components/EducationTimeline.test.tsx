/**
 * Tests for EducationTimeline.
 *
 * Covers:
 *   - sort order (start_year desc, end_year desc tiebreak)
 *   - empty array placeholder
 *   - school link target/rel correctness
 *   - diamond marker rendered
 *   - degree + field_of_study render conditionally
 *   - maxRows + Show all expand
 *   - stable when start_year is null
 *   - year-only formatter output ("2010 – 2014 · 5 yrs")
 *   - formatDateRange contract cases (delegated to shared formatter)
 *   - sortEducationDesc purity (no input mutation)
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import {
  EducationTimeline,
  formatDateRange,
  sortEducationDesc,
  type EducationPeriod,
} from "@/components/EducationTimeline";

afterEach(() => {
  cleanup();
});

const sample = (overrides: Partial<EducationPeriod> = {}): EducationPeriod => ({
  school_name: "MIT",
  ...overrides,
});

describe("formatDateRange (EducationTimeline)", () => {
  it("supports the same Mar 2018 – Aug 2024 case as CareerTimeline", () => {
    const out = formatDateRange(2018, 3, 2024, 8, false);
    expect(out).toBe("Mar 2018 – Aug 2024 · 6 yrs 6 mos");
  });

  it("returns 'Date unknown' when start_year is null", () => {
    expect(formatDateRange(null, null, 2024, null, false)).toBe("Date unknown");
  });

  it("returns 'Mar 2018 – ? · ongoing' when end is missing and not current", () => {
    expect(formatDateRange(2018, 3, null, null, false)).toBe(
      "Mar 2018 – ? · ongoing",
    );
  });
});

describe("sortEducationDesc", () => {
  it("sorts desc by start_year", () => {
    const rows: EducationPeriod[] = [
      sample({ school_name: "Old", start_year: 2010 }),
      sample({ school_name: "New", start_year: 2020 }),
      sample({ school_name: "Mid", start_year: 2015 }),
    ];
    const out = sortEducationDesc(rows).map((r) => r.school_name);
    expect(out).toEqual(["New", "Mid", "Old"]);
  });

  it("ties on start_year break by end_year desc", () => {
    const rows: EducationPeriod[] = [
      sample({ school_name: "Short", start_year: 2010, end_year: 2012 }),
      sample({ school_name: "Long", start_year: 2010, end_year: 2016 }),
    ];
    const out = sortEducationDesc(rows).map((r) => r.school_name);
    expect(out).toEqual(["Long", "Short"]);
  });

  it("rows with null start_year sort to the end", () => {
    const rows: EducationPeriod[] = [
      sample({ school_name: "Null", start_year: null }),
      sample({ school_name: "Real", start_year: 2018 }),
    ];
    const out = sortEducationDesc(rows).map((r) => r.school_name);
    expect(out).toEqual(["Real", "Null"]);
  });

  it("does not mutate the input array", () => {
    const rows: EducationPeriod[] = [
      sample({ school_name: "A", start_year: 2010 }),
      sample({ school_name: "B", start_year: 2020 }),
    ];
    const snapshot = rows.slice();
    sortEducationDesc(rows);
    expect(rows).toEqual(snapshot);
  });
});

describe("<EducationTimeline />", () => {
  it("renders empty placeholder for an empty array", () => {
    render(<EducationTimeline education={[]} />);
    expect(screen.getByTestId("education-timeline-empty")).toHaveTextContent(
      /No education history available\./i,
    );
  });

  it("renders one row per education period in correct order", () => {
    const education: EducationPeriod[] = [
      sample({ school_name: "Old U", start_year: 2010 }),
      sample({ school_name: "New U", start_year: 2020 }),
    ];
    render(<EducationTimeline education={education} />);
    const rows = screen.getAllByTestId("education-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("New U");
    expect(rows[1]).toHaveTextContent("Old U");
  });

  it("renders school as link with target=_blank + rel='noopener noreferrer'", () => {
    const education: EducationPeriod[] = [
      sample({
        school_name: "MIT",
        school_linkedin_url: "https://linkedin.com/school/mit",
        start_year: 2010,
        end_year: 2014,
      }),
    ];
    render(<EducationTimeline education={education} />);
    const link = screen.getByTestId("education-school-link") as HTMLAnchorElement;
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toMatch(/noopener/);
    expect(link.getAttribute("rel")).toMatch(/noreferrer/);
    expect(link.getAttribute("href")).toBe("https://linkedin.com/school/mit");
  });

  it("renders school as plain text when no URL provided", () => {
    render(
      <EducationTimeline
        education={[sample({ school_name: "Plain U", start_year: 2010 })]}
      />,
    );
    expect(screen.getByTestId("education-school-text")).toHaveTextContent("Plain U");
    expect(screen.queryByTestId("education-school-link")).toBeNull();
  });

  it("renders diamond markers (rotate-45 css class)", () => {
    render(
      <EducationTimeline
        education={[sample({ school_name: "MIT", start_year: 2010 })]}
      />,
    );
    const marker = screen.getByTestId("education-marker-diamond");
    expect(marker.className).toMatch(/rotate-45/);
  });

  it("renders degree and field_of_study when provided", () => {
    render(
      <EducationTimeline
        education={[
          sample({
            school_name: "MIT",
            start_year: 2010,
            end_year: 2014,
            degree: "BS",
            field_of_study: "EECS",
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("education-row")).toHaveTextContent("BS");
    expect(screen.getByTestId("education-row")).toHaveTextContent("EECS");
  });

  it("renders year-only date range like '2010 – 2014 · 5 yrs'", () => {
    render(
      <EducationTimeline
        education={[
          sample({ school_name: "MIT", start_year: 2010, end_year: 2014 }),
        ]}
      />,
    );
    // Inclusive year math: 2010..2014 = 5 yrs.
    expect(screen.getByTestId("education-row")).toHaveTextContent(
      "2010 – 2014 · 5 yrs",
    );
  });

  it("respects maxRows and reveals remaining rows on 'Show all' click", () => {
    const education: EducationPeriod[] = [
      sample({ school_name: "A", start_year: 2024 }),
      sample({ school_name: "B", start_year: 2022 }),
      sample({ school_name: "C", start_year: 2020 }),
    ];
    render(<EducationTimeline education={education} maxRows={1} />);
    expect(screen.getAllByTestId("education-row")).toHaveLength(1);
    const button = screen.getByTestId("education-show-all");
    expect(button).toHaveTextContent("Show all (3)");
    fireEvent.click(button);
    expect(screen.getAllByTestId("education-row")).toHaveLength(3);
    expect(screen.queryByTestId("education-show-all")).toBeNull();
  });

  it("does not show 'Show all' when count <= maxRows", () => {
    render(
      <EducationTimeline
        education={[sample({ school_name: "A", start_year: 2020 })]}
        maxRows={5}
      />,
    );
    expect(screen.queryByTestId("education-show-all")).toBeNull();
  });
});
