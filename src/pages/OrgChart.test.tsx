/**
 * Tests for the standalone OrgChart page (/org/:companyId).
 *
 * Mocking strategy mirrors ProspectDetail.test.tsx:
 *   - react-router-dom: useParams + useNavigate are stubbed.
 *   - reactflow: replaced with a deterministic stand-in that exposes
 *     nodes/edges via data-testids so we can assert counts and click
 *     handlers without needing canvas + ResizeObserver.
 *   - @/lib/supabase: chainable mock whose results are queued via
 *     `supabaseQueue`. Each await pops one entry.
 *   - @/components/OrgCorrectionDialog: replaced with a tiny spy component
 *     so we can assert the dialog opened and inspect its props.
 *
 * The dynamic-imported modules `@/components/CompanyHeaderCard` and
 * `@/lib/orgClusters` do not exist at write time. The page handles their
 * absence with try/catch + Suspense fallback. We don't mock them in tests —
 * the real "module not found" path is exercised, which is the production
 * behavior we care about.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  act,
  waitFor,
} from "@testing-library/react";

// ── Mocks (must be hoisted before component import) ────────────────────────

const mockNavigate = vi.fn();

vi.mock("react-router-dom", () => ({
  useParams: () => ({ companyId: "company-1" }),
  useNavigate: () => mockNavigate,
}));

// reactflow stand-in: render every node by looking up its type in nodeTypes,
// expose stable testids for click assertions and edge counts.
vi.mock("reactflow", () => {
  const ReactFlow = (props: {
    nodes?: unknown[];
    edges?: unknown[];
    nodeTypes?: Record<string, React.ComponentType<unknown>>;
    onNodeClick?: (e: unknown, node: unknown) => void;
    onEdgeClick?: (e: unknown, edge: unknown) => void;
    onPaneClick?: () => void;
  }) => {
    const nodes = (props.nodes ?? []) as Array<{
      id: string;
      type?: string;
      data?: Record<string, unknown>;
    }>;
    const edges = (props.edges ?? []) as Array<{
      id: string;
      source: string;
      target: string;
      style?: { strokeDasharray?: string; strokeWidth?: number };
      data?: Record<string, unknown>;
    }>;
    const NodeTypes = props.nodeTypes ?? {};
    return (
      <div data-testid="rf" onClick={() => props.onPaneClick?.()}>
        <div data-testid="rf-node-count">{nodes.length}</div>
        <div data-testid="rf-edge-count">{edges.length}</div>
        {nodes.map((n) => {
          const Comp = n.type ? NodeTypes[n.type] : null;
          return (
            <div
              key={n.id}
              data-testid={`rf-node-${n.id}`}
              onClick={(e) => {
                e.stopPropagation();
                props.onNodeClick?.(e, n);
              }}
            >
              {Comp ? (
                <Comp {...({ data: n.data } as unknown as Record<string, unknown>)} />
              ) : (
                <span>{n.id}</span>
              )}
            </div>
          );
        })}
        {edges.map((e) => (
          <div
            key={e.id}
            data-testid={`rf-edge-${e.id}`}
            data-stroke-dasharray={e.style?.strokeDasharray ?? ""}
            data-stroke-width={e.style?.strokeWidth ?? ""}
            onClick={(ev) => {
              ev.stopPropagation();
              props.onEdgeClick?.(ev, e);
            }}
          >
            edge {e.id}
          </div>
        ))}
      </div>
    );
  };
  return {
    default: ReactFlow,
    Background: () => null,
    Controls: () => null,
  };
});

// OrgCorrectionDialog spy — exposes its props on a global hook so tests can
// assert what was passed in.
const correctionDialogProps: Array<Record<string, unknown>> = [];
vi.mock("@/components/OrgCorrectionDialog", () => ({
  OrgCorrectionDialog: (props: Record<string, unknown>) => {
    correctionDialogProps.push(props);
    if (!props.open) return null;
    return (
      <div data-testid="org-correction-dialog">
        dialog open for {String(props.personAName)}
      </div>
    );
  },
}));

// Supabase mock: chainable shape; each terminal `await` pops one entry from
// `supabaseQueue`. The shape covers .select().eq().eq().order().limit(),
// .select().eq().maybeSingle(), and .select().in().
let supabaseQueue: Array<{ data: unknown; error: unknown }> = [];

vi.mock("@/lib/supabase", () => {
  const makeChain = () => {
    const self: Record<string, unknown> = {};
    const ret = () => self;
    self.select = ret;
    self.eq = ret;
    self.in = ret;
    self.order = ret;
    self.limit = ret;
    self.maybeSingle = () =>
      Promise.resolve(supabaseQueue.shift() ?? { data: null, error: null });
    self.then = (
      onF: (v: { data: unknown; error: unknown }) => unknown,
      onR?: (e: unknown) => unknown,
    ) =>
      Promise.resolve(supabaseQueue.shift() ?? { data: [], error: null }).then(
        onF,
        onR,
      );
    return self;
  };
  return {
    supabase: { from: () => makeChain() },
    HAS_REAL_SUPABASE: true,
    ENABLE_ORG_CHART: true,
  };
});

// Now import the module under test (after mocks).
import OrgChart from "./OrgChart";

// ── Fixtures ───────────────────────────────────────────────────────────────

const COMPANY = {
  id: "company-1",
  canonical_name: "Acme Corp",
  industry: "Semiconductors",
  hq_country: "US",
  enriched_count: 42,
};

const PERSONS = [
  {
    id: "p-ceo",
    canonical_name: "Alice Stone",
    current_title: "CEO",
    current_seniority_score: 100,
    current_functional_domain: "general_management",
    is_unresolved_target: false,
  },
  {
    id: "p-vp-eng",
    canonical_name: "Bob Lin",
    current_title: "VP Engineering",
    current_seniority_score: 80,
    current_functional_domain: "hardware_engineering",
    is_unresolved_target: false,
  },
  {
    id: "p-stub",
    canonical_name: "[Unknown VP of Sales]",
    current_title: "VP Sales",
    current_seniority_score: 78,
    current_functional_domain: "sales_marketing",
    is_unresolved_target: true,
  },
];

const EDGES = [
  {
    id: "e-1",
    manager_id: "p-ceo",
    report_id: "p-vp-eng",
    confidence: 0.9,
    path_confidence: 0.85,
    inference_method: "linkedin_reports_to",
    is_current: true,
    valid_from: "2024-01-01",
    valid_to: null,
  },
  {
    id: "e-2",
    manager_id: "p-ceo",
    report_id: "p-stub",
    confidence: 0.5,
    path_confidence: 0.5,
    inference_method: "job_posting_nlp",
    is_current: true,
    valid_from: "2024-06-01",
    valid_to: null,
  },
  {
    id: "e-3-historical",
    manager_id: "p-ceo",
    report_id: "p-vp-eng",
    confidence: 0.6,
    path_confidence: 0.55,
    inference_method: "implicit_scoring",
    is_current: false,
    valid_from: "2018-01-01",
    valid_to: "2023-12-31",
  },
];

/**
 * Queue the standard 6-call response sequence in order:
 *   1. companies.maybeSingle             → company row
 *   2. employment_periods                → person ids at company
 *   3. org_reporting_edges               → all edges
 *   4. persons (endpoint metadata)       → person rows
 *   5. org_functional_clusters           → []
 *   6. org_cluster_members               → [] (skipped if no clusters)
 *   7. persons (top 5)                   → top persons
 */
function queueStandardLoad(opts: {
  edges?: typeof EDGES;
  persons?: typeof PERSONS;
  hasClusters?: boolean;
} = {}) {
  const edges = opts.edges ?? EDGES;
  const persons = opts.persons ?? PERSONS;
  // 1. company maybeSingle
  supabaseQueue.push({ data: COMPANY, error: null });
  // 2. employment_periods
  supabaseQueue.push({
    data: persons.map((p) => ({ person_id: p.id })),
    error: null,
  });
  // 3. org_reporting_edges
  supabaseQueue.push({ data: edges, error: null });
  // 4. persons by id
  supabaseQueue.push({ data: persons, error: null });
  // 5. org_functional_clusters
  if (opts.hasClusters) {
    supabaseQueue.push({
      data: [
        {
          id: "c-1",
          functional_domain: "general_management",
          sub_domain: null,
          company_id: COMPANY.id,
        },
      ],
      error: null,
    });
    // 6. org_cluster_members
    supabaseQueue.push({
      data: persons.map((p) => ({ cluster_id: "c-1", person_id: p.id })),
      error: null,
    });
  } else {
    supabaseQueue.push({ data: [], error: null });
    // No org_cluster_members fetch happens when clusters is empty — but be
    // safe and queue an empty result so any extra await doesn't pull from a
    // later test's queue.
    supabaseQueue.push({ data: [], error: null });
  }
  // 7. top persons
  supabaseQueue.push({
    data: persons.slice(0, 5).map((p) => ({
      id: p.id,
      canonical_name: p.canonical_name,
      current_title: p.current_title,
      current_seniority_score: p.current_seniority_score,
    })),
    error: null,
  });
}

async function flushAsync() {
  // Let queued promises resolve, including the lazy import attempts.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
  });
}

// ── Tests ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  supabaseQueue = [];
  correctionDialogProps.length = 0;
  mockNavigate.mockReset();
  cleanup();
});

describe("OrgChart page", () => {
  it("renders header (or fallback) with the company name from URL params", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() =>
      expect(screen.getByText(COMPANY.canonical_name)).toBeInTheDocument(),
    );
    // The fallback rendered from <Suspense> is fine — both paths show the
    // canonical name. We assert the testid for either the lazy module or the
    // built-in placeholder.
    const placeholder = screen.queryByTestId("company-header-fallback");
    expect(placeholder).not.toBeNull();
  });

  it("renders one ReactFlow node per unique person referenced by edges", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-node-count"));
    // 3 unique persons across e-1 + e-2 (edge-3 historical filtered out by
    // default since showHistorical=false at first render).
    expect(screen.getByTestId("rf-node-count").textContent).toBe("3");
  });

  it("renders one ReactFlow edge per row in org_reporting_edges (current only)", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-edge-count"));
    expect(screen.getByTestId("rf-edge-count").textContent).toBe("2");
  });

  it("'Functional clusters' toggle changes node fill color from domain palette to neutral gray", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-node-count"));

    // With clusters ON, the CEO (general_management) node should NOT use the
    // neutral gray fill. We can't read the inline style without rendering
    // PersonCircleNode — but our reactflow stub does render nodeTypes
    // components, so we can assert.
    const ceoNodeOn = screen.getByTestId("person-node-p-ceo");
    const fillOn = (ceoNodeOn as HTMLElement).style.background;
    expect(fillOn).not.toBe("");

    // Toggle clusters off.
    fireEvent.click(screen.getByTestId("toggle-clusters"));

    const ceoNodeOff = screen.getByTestId("person-node-p-ceo");
    const fillOff = (ceoNodeOff as HTMLElement).style.background;
    // Neutral fallback is "rgb(156, 163, 175)" or "#9CA3AF" depending on
    // serialization. Just assert it differs from the clusters-on color.
    expect(fillOff).not.toBe(fillOn);
  });

  it("'Show historical edges' toggle includes is_current=false rows when on", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-edge-count"));

    expect(screen.getByTestId("rf-edge-count").textContent).toBe("2");
    fireEvent.click(screen.getByTestId("toggle-historical"));
    await waitFor(() =>
      expect(screen.getByTestId("rf-edge-count").textContent).toBe("3"),
    );
  });

  it("search box filters/highlights matching nodes (non-matches render dimmed)", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-node-count"));

    fireEvent.change(screen.getByTestId("search-input"), {
      target: { value: "Bob" },
    });

    await waitFor(() => {
      const matched = screen.getByTestId("person-node-p-vp-eng") as HTMLElement;
      const unmatched = screen.getByTestId("person-node-p-ceo") as HTMLElement;
      expect(matched.style.opacity).toBe("1");
      expect(unmatched.style.opacity).toBe("0.4");
    });
  });

  it("clicking a person node calls navigate('/prospect/:id')", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-node-p-ceo"));

    fireEvent.click(screen.getByTestId("rf-node-p-ceo"));
    expect(mockNavigate).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith("/prospect/p-ceo");
  });

  it("clicking an edge opens OrgCorrectionDialog with the edge's data", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-edge-e-1"));

    fireEvent.click(screen.getByTestId("rf-edge-e-1"));

    await waitFor(() =>
      expect(screen.getByTestId("org-correction-dialog")).toBeInTheDocument(),
    );

    // Verify the spy received the correct edge id + person ids.
    const lastProps = correctionDialogProps[correctionDialogProps.length - 1];
    expect(lastProps.open).toBe(true);
    expect(lastProps.defaultEdgeId).toBe("e-1");
    expect(lastProps.personAId).toBe("p-vp-eng");
    expect(lastProps.defaultPersonBId).toBe("p-ceo");
  });

  it("renders the unresolved diamond placeholder for is_unresolved_target persons (Decision 4)", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("unresolved-node-p-stub"));

    const stub = screen.getByTestId("unresolved-node-p-stub") as HTMLElement;
    expect(stub.getAttribute("data-unresolved")).toBe("true");

    // And clicking an unresolved node should NOT navigate (Decision 4).
    fireEvent.click(screen.getByTestId("rf-node-p-stub"));
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("renders an empty-state placeholder when there are 0 reporting edges", async () => {
    // Queue: company, employment_periods, edges (empty), then early return —
    // but the loader still always queues 7 entries to avoid sequence drift.
    supabaseQueue.push({ data: COMPANY, error: null });
    supabaseQueue.push({
      data: PERSONS.map((p) => ({ person_id: p.id })),
      error: null,
    });
    supabaseQueue.push({ data: [], error: null }); // org_reporting_edges
    // After empty edges the page still queries persons (empty endpointIds →
    // no fetch happens). The clusters branch always runs.
    supabaseQueue.push({ data: [], error: null }); // org_functional_clusters
    supabaseQueue.push({ data: [], error: null }); // org_cluster_members spare
    supabaseQueue.push({ data: [], error: null }); // top persons

    render(<OrgChart />);
    await flushAsync();
    await waitFor(() =>
      expect(screen.getByTestId("org-chart-empty")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/No reporting edges found for/i),
    ).toBeInTheDocument();
  });

  it("clicking the canvas (pane click) clears the selected edge", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-edge-e-1"));

    fireEvent.click(screen.getByTestId("rf-edge-e-1"));
    await waitFor(() =>
      expect(screen.getByTestId("org-correction-dialog")).toBeInTheDocument(),
    );

    // Pane click — our stub fires onPaneClick when the rf root is clicked.
    fireEvent.click(screen.getByTestId("rf"));
    // Selection clears on pane click; dialog stays open until user closes it
    // (per spec). Just verify the side-panel selected-edge UI clears.
    // The side panel renders "Click an edge to inspect…" placeholder when no
    // edge selected.
    await waitFor(() =>
      expect(
        screen.getByText(/Click an edge to inspect/i),
      ).toBeInTheDocument(),
    );
  });

  it("solid edge style for high-confidence (>=0.7), dashed for medium (0.4-0.7)", async () => {
    queueStandardLoad();
    render(<OrgChart />);
    await flushAsync();
    await waitFor(() => screen.getByTestId("rf-edge-e-1"));

    const highConf = screen.getByTestId("rf-edge-e-1") as HTMLElement;
    expect(highConf.getAttribute("data-stroke-dasharray")).toBe("");

    const mediumConf = screen.getByTestId("rf-edge-e-2") as HTMLElement;
    expect(mediumConf.getAttribute("data-stroke-dasharray")).toBe("6 4");
  });
});
