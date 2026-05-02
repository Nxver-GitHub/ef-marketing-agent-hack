/**
 * Tests for CompanyHeaderCard. Pure presentational, no mocks needed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import {
  CompanyHeaderCard,
  type CompanyHeaderCardCompany,
  type CompanyHeaderCardTopPerson,
  progressPercent,
  confidenceLabel,
} from "./CompanyHeaderCard";

const baseCompany: CompanyHeaderCardCompany = {
  id: "co-1",
  canonical_name: "Acme Inc",
};

beforeEach(() => {
  cleanup();
});

describe("progressPercent", () => {
  it("returns rounded percentage", () => {
    expect(progressPercent(125, 500)).toBe("25%");
    expect(progressPercent(500, 500)).toBe("100%");
    expect(progressPercent(0, 500)).toBe("0%");
  });

  it("clamps over 100% at 100%", () => {
    expect(progressPercent(800, 500)).toBe("100%");
  });

  it("returns empty string for invalid inputs", () => {
    expect(progressPercent(NaN, 500)).toBe("");
    expect(progressPercent(100, 0)).toBe("");
    expect(progressPercent(100, -1)).toBe("");
    // Defensive: null at runtime even though the type forbids it.
    expect(progressPercent(null as unknown as number, 500)).toBe("");
  });
});

describe("confidenceLabel", () => {
  it("formats float [0,1] as percentage", () => {
    expect(confidenceLabel(0.72)).toBe("72%");
    expect(confidenceLabel(0)).toBe("0%");
    expect(confidenceLabel(1)).toBe("100%");
  });

  it("clamps out-of-range values", () => {
    expect(confidenceLabel(1.5)).toBe("100%");
    expect(confidenceLabel(-0.2)).toBe("0%");
  });

  it("returns empty string for null / undefined / NaN", () => {
    expect(confidenceLabel(null)).toBe("");
    expect(confidenceLabel(undefined)).toBe("");
    expect(confidenceLabel(NaN)).toBe("");
  });
});

describe("CompanyHeaderCard rendering", () => {
  it("renders the company name as the heading", () => {
    render(<CompanyHeaderCard company={baseCompany} enriched_count={100} />);
    expect(screen.getByRole("heading", { name: "Acme Inc" })).toBeInTheDocument();
  });

  it("renders industry + flag when both provided", () => {
    render(
      <CompanyHeaderCard
        company={{ ...baseCompany, industry: "Semiconductors", hq_country: "US" }}
        enriched_count={100}
      />,
    );
    expect(screen.getByText("Semiconductors")).toBeInTheDocument();
    expect(screen.getByLabelText("US")).toBeInTheDocument();
  });

  it("omits industry / flag when both null", () => {
    render(<CompanyHeaderCard company={baseCompany} enriched_count={100} />);
    expect(screen.queryByText("Semiconductors")).toBeNull();
  });

  it("renders enrichment progress with default 500 target", () => {
    render(<CompanyHeaderCard company={baseCompany} enriched_count={250} />);
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("50");
    expect(screen.getByText(/250\/500/)).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
  });

  it("respects custom target_count", () => {
    render(
      <CompanyHeaderCard
        company={baseCompany}
        enriched_count={300}
        target_count={1000}
      />,
    );
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("30");
  });

  it("renders domains as chips when present", () => {
    render(
      <CompanyHeaderCard
        company={{ ...baseCompany, domains: ["acme.com", "acme.io"] }}
        enriched_count={100}
      />,
    );
    expect(screen.getByText("acme.com")).toBeInTheDocument();
    expect(screen.getByText("acme.io")).toBeInTheDocument();
  });

  it("filters empty / non-string domain entries", () => {
    render(
      <CompanyHeaderCard
        company={{
          ...baseCompany,
          // Cast: real Postgres rows can return null entries inside the
          // text[] domain array if the column was inserted with sparse
          // values. This test exercises the runtime guard in the component.
          domains: ["acme.com", "", null, "foo.com"] as unknown as string[],
        }}
        enriched_count={100}
      />,
    );
    expect(screen.getByText("acme.com")).toBeInTheDocument();
    expect(screen.getByText("foo.com")).toBeInTheDocument();
    // empty/null filtered: only 2 chips render
    expect(screen.queryAllByText(/acme|foo/).length).toBe(2);
  });

  it("renders org-chart confidence + signal count when provided", () => {
    render(
      <CompanyHeaderCard
        company={{
          ...baseCompany,
          org_chart_confidence: 0.83,
          org_chart_signal_count: 1247,
        }}
        enriched_count={100}
      />,
    );
    expect(screen.getByText("83%")).toBeInTheDocument();
    expect(screen.getByText(/1,247 signals/)).toBeInTheDocument();
  });

  it("limits top_persons to 5 even when more passed", () => {
    const persons: CompanyHeaderCardTopPerson[] = Array.from(
      { length: 8 },
      (_, i) => ({ id: `p${i}`, canonical_name: `Person ${i}` }),
    );
    render(
      <CompanyHeaderCard
        company={baseCompany}
        enriched_count={100}
        top_persons={persons}
      />,
    );
    expect(screen.getByText("Person 0")).toBeInTheDocument();
    expect(screen.getByText("Person 4")).toBeInTheDocument();
    expect(screen.queryByText("Person 5")).toBeNull();
  });

  it("fires onPersonClick with the clicked person id", () => {
    const onPersonClick = vi.fn();
    render(
      <CompanyHeaderCard
        company={baseCompany}
        enriched_count={100}
        top_persons={[
          { id: "p1", canonical_name: "Alice" },
          { id: "p2", canonical_name: "Bob" },
        ]}
        onPersonClick={onPersonClick}
      />,
    );
    fireEvent.click(screen.getByTestId("top-person-p2"));
    expect(onPersonClick).toHaveBeenCalledWith("p2");
  });

  it("renders person score when finite", () => {
    render(
      <CompanyHeaderCard
        company={baseCompany}
        enriched_count={100}
        top_persons={[
          { id: "p1", canonical_name: "Alice", score: 87.4 },
        ]}
      />,
    );
    expect(screen.getByText("87")).toBeInTheDocument();
  });
});
