/**
 * Tests for EdgeInspector.
 *
 * Covers:
 *   1. Null edge → empty state placeholder
 *   2. Edge present → both names in header
 *   3. Strength bar color thresholds (>=0.7 green, 0.4-0.7 yellow, <0.4 gray)
 *   4. All 4 factor cells rendered
 *   5. "—" shown for null factors
 *   6. Each evidence type renders its specific template (patent/paper/career/standards/conference/cohort/unknown)
 *   7. Patent template "View on USPTO" link only when url present
 *   8. Paper template shows citation count and DOI when present
 *   9. Unknown source_type falls back to JSON snippet
 *  10. onDismiss called when close clicked
 *  11. onUseConnection called with edge when "Use this" clicked
 *  12. No buttons when callbacks not provided
 *  13. className passed through
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { EdgeInspector, type EdgeInspectorEdge } from "./EdgeInspector";

afterEach(() => cleanup());

const baseEdge = (overrides: Partial<EdgeInspectorEdge> = {}): EdgeInspectorEdge => ({
  id: "e1",
  connection_type: "patent_co_inventor",
  base_strength: 0.95,
  recency_factor: 0.92,
  frequency_factor: 1.1,
  corroboration_factor: 1.2,
  computed_strength: 0.85,
  evidence: [],
  source_person: {
    id: "p_src",
    canonical_name: "Sarah Kim",
    current_title: "VP Eng",
    current_company_name: "Acme",
  },
  target_person: {
    id: "p_tgt",
    canonical_name: "Wei Chen",
    current_title: "Director",
    current_company_name: "Globex",
  },
  ...overrides,
});

describe("EdgeInspector — empty state", () => {
  it("renders placeholder when edge is null", () => {
    render(<EdgeInspector edge={null} />);
    expect(screen.getByText(/click an edge to see its evidence/i)).toBeInTheDocument();
  });

  it("passes className through on empty state", () => {
    const { container } = render(
      <EdgeInspector edge={null} className="empty-cls" />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("empty-cls");
  });
});

describe("EdgeInspector — header", () => {
  it("renders both source and target person names", () => {
    render(<EdgeInspector edge={baseEdge()} />);
    expect(screen.getByText("Sarah Kim")).toBeInTheDocument();
    expect(screen.getByText("Wei Chen")).toBeInTheDocument();
  });

  it("renders 'via {connection_type}' subline (humanized)", () => {
    render(<EdgeInspector edge={baseEdge()} />);
    expect(screen.getByText(/via patent co inventor/i)).toBeInTheDocument();
  });
});

describe("EdgeInspector — strength bar color", () => {
  it("uses green for computed_strength >= 0.7", () => {
    render(<EdgeInspector edge={baseEdge({ computed_strength: 0.85 })} />);
    const fill = screen.getByTestId("edge-strength-bar-fill");
    expect(fill.className).toContain("emerald");
  });

  it("uses yellow/amber for 0.4 <= computed_strength < 0.7", () => {
    render(<EdgeInspector edge={baseEdge({ computed_strength: 0.55 })} />);
    const fill = screen.getByTestId("edge-strength-bar-fill");
    expect(fill.className).toContain("amber");
  });

  it("uses gray for computed_strength < 0.4", () => {
    render(<EdgeInspector edge={baseEdge({ computed_strength: 0.2 })} />);
    const fill = screen.getByTestId("edge-strength-bar-fill");
    expect(fill.className).toContain("muted-foreground");
  });
});

describe("EdgeInspector — factor cells", () => {
  it("renders all 4 factor cells with formatted values", () => {
    render(<EdgeInspector edge={baseEdge()} />);
    expect(screen.getByText("Base")).toBeInTheDocument();
    expect(screen.getByText("Recency")).toBeInTheDocument();
    expect(screen.getByText("Frequency")).toBeInTheDocument();
    expect(screen.getByText("Corrob.")).toBeInTheDocument();
    expect(screen.getByText("0.95")).toBeInTheDocument();
    expect(screen.getByText("0.92")).toBeInTheDocument();
    expect(screen.getByText("1.10")).toBeInTheDocument();
    expect(screen.getByText("1.20")).toBeInTheDocument();
  });

  it("shows '—' for null factor values", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          recency_factor: null,
          frequency_factor: null,
          corroboration_factor: null,
        })}
      />,
    );
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBe(3);
  });
});

describe("EdgeInspector — evidence templates", () => {
  it("renders patent template with patent number and title", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "patent",
              structured_value: {
                patent_number: "10,234,567",
                patent_title: "Method for tight-coupled memory",
                assignee: "Intel",
                year: "2018",
              },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Patent 10,234,567/)).toBeInTheDocument();
    expect(screen.getByText(/tight-coupled memory/)).toBeInTheDocument();
    expect(screen.getByText(/Intel.*2018/)).toBeInTheDocument();
  });

  it("renders patent 'View on USPTO' link only when url is present", () => {
    const { unmount } = render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "patent",
              structured_value: { patent_number: "10,000,001" },
            },
          ],
        })}
      />,
    );
    expect(screen.queryByText(/View on USPTO/i)).not.toBeInTheDocument();
    unmount();

    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "patent",
              url: "https://patft.uspto.gov/12345",
              structured_value: { patent_number: "12,345,678" },
            },
          ],
        })}
      />,
    );
    const link = screen.getByText(/View on USPTO/i).closest("a");
    expect(link).toHaveAttribute("href", "https://patft.uspto.gov/12345");
  });

  it("renders paper template with citation count and DOI", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "paper",
              structured_value: {
                paper_title: "Attention Is All You Need",
                venue: "NeurIPS",
                year: 2017,
                citation_count: 9999,
                doi: "10.5555/abc.123",
              },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Attention Is All You Need/)).toBeInTheDocument();
    expect(screen.getByText(/9999 citations/)).toBeInTheDocument();
    const doiLink = screen.getByText(/10\.5555\/abc\.123/).closest("a");
    expect(doiLink).toHaveAttribute("href", "https://doi.org/10.5555/abc.123");
  });

  it("renders career_overlap template with company and years", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "career_overlap",
              structured_value: {
                company: "Intel",
                start_year: 2015,
                end_year: 2019,
                same_team: true,
                team: "Memory Architecture",
              },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Both at Intel/)).toBeInTheDocument();
    expect(screen.getByText(/from 2015/)).toBeInTheDocument();
    expect(screen.getByText(/to 2019/)).toBeInTheDocument();
    expect(screen.getByText(/Same team.*Memory Architecture/)).toBeInTheDocument();
  });

  it("renders standards template with organization, committee, role, years", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "standards",
              structured_value: {
                organization: "JEDEC",
                committee: "JC-42.4",
                role: "Voting member",
                years: "2018-2022",
              },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/JEDEC.*JC-42\.4/)).toBeInTheDocument();
    expect(screen.getByText(/Voting member.*2018-2022/)).toBeInTheDocument();
  });

  it("renders conference template with event and year", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "conference",
              structured_value: { event: "ISSCC", year: 2024 },
            },
          ],
        })}
      />,
    );
    expect(
      screen.getByText(/Co-presented at ISSCC.*2024/),
    ).toBeInTheDocument();
  });

  it("renders cohort template with school, degree, year_overlap", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "cohort",
              structured_value: {
                school: "Stanford",
                degree: "PhD CS",
                year_overlap: "2010-2014",
              },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Stanford/)).toBeInTheDocument();
    expect(screen.getByText(/PhD CS/)).toBeInTheDocument();
    expect(screen.getByText(/overlap: 2010-2014/)).toBeInTheDocument();
  });

  it("falls back to JSON snippet for unknown source_type", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "unknown",
              structured_value: { foo: "bar", n: 42 },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/"foo":"bar"/)).toBeInTheDocument();
  });

  it("truncates JSON snippet to 200 chars", () => {
    const big = "x".repeat(500);
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            {
              source_type: "unknown",
              structured_value: { big },
            },
          ],
        })}
      />,
    );
    const node = screen.getByText(/^\{"big":/);
    expect(node.textContent?.length).toBeLessThanOrEqual(200);
  });
});

describe("EdgeInspector — actions", () => {
  it("calls onDismiss when close button clicked", () => {
    const onDismiss = vi.fn();
    render(<EdgeInspector edge={baseEdge()} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByLabelText(/close edge inspector/i));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("calls onUseConnection with edge when 'Use this connection' clicked", () => {
    const edge = baseEdge();
    const onUseConnection = vi.fn();
    render(<EdgeInspector edge={edge} onUseConnection={onUseConnection} />);
    fireEvent.click(screen.getByText(/use this connection/i));
    expect(onUseConnection).toHaveBeenCalledTimes(1);
    expect(onUseConnection).toHaveBeenCalledWith(edge);
  });

  it("does not render close or use buttons when callbacks not provided", () => {
    render(<EdgeInspector edge={baseEdge()} />);
    expect(
      screen.queryByLabelText(/close edge inspector/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/use this connection/i)).not.toBeInTheDocument();
  });

  it("passes className through to the panel", () => {
    const { container } = render(
      <EdgeInspector edge={baseEdge()} className="custom-edge-cls" />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("custom-edge-cls");
  });
});

describe("EdgeInspector — evidence count", () => {
  it("shows the count of evidence sources in the section header", () => {
    render(
      <EdgeInspector
        edge={baseEdge({
          evidence: [
            { source_type: "patent", structured_value: { patent_number: "1" } },
            { source_type: "paper", structured_value: { paper_title: "p" } },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Evidence \(2 sources\)/i)).toBeInTheDocument();
  });

  it("renders '0 sources' and a placeholder when evidence array empty", () => {
    render(<EdgeInspector edge={baseEdge({ evidence: [] })} />);
    expect(screen.getByText(/Evidence \(0 sources\)/i)).toBeInTheDocument();
    expect(
      screen.getByText(/no structured evidence attached/i),
    ).toBeInTheDocument();
  });
});

describe("EdgeInspector — computed_strength formatting", () => {
  it("clamps and formats computed_strength to 2 decimals", () => {
    render(<EdgeInspector edge={baseEdge({ computed_strength: 0.857 })} />);
    expect(screen.getByText("0.86")).toBeInTheDocument();
  });

  it("hard-caps display at 0.99", () => {
    render(<EdgeInspector edge={baseEdge({ computed_strength: 1.5 })} />);
    expect(screen.getByText("0.99")).toBeInTheDocument();
  });
});
