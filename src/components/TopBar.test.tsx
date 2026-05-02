/**
 * TopBar — conditional EdgeFilterPills rendering by route.
 *
 * Catches the bug class user flagged in C2: a wired component that
 * exists in the tree but isn't rendered for the current route.
 *
 * EdgeFilterPills should appear ONLY on /discover and /org routes —
 * the routes where toggling edge visibility is meaningful.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("@/contexts/AccountContext", () => ({
  useAccount: () => ({
    account: { id: "demo-account", displayName: "Demo" },
    user: null,
    loading: false,
    signOut: vi.fn(),
  }),
}));

// EdgeFilterPills isn't the focus here — replace with a sentinel so we
// can assert presence/absence without needing the full pill render.
vi.mock("@/components/EdgeFilterPills", () => ({
  EdgeFilterPills: () => <div data-testid="edge-filter-pills-sentinel" />,
}));

const locationMock = vi.hoisted(() => ({ pathname: "/discover" }));

vi.mock("react-router-dom", () => ({
  Link: ({ children, ...rest }: { children: React.ReactNode } & Record<string, unknown>) => (
    <a {...rest}>{children}</a>
  ),
  useLocation: () => locationMock,
}));

beforeEach(() => {
  cleanup();
});

async function renderTopBarAt(pathname: string) {
  locationMock.pathname = pathname;
  // Re-import after mocks settle so the component picks up the mocked
  // useLocation each test.
  const { TopBar } = await import("./TopBar");
  render(<TopBar />);
}

describe("TopBar — EdgeFilterPills route gating", () => {
  it("renders the filter pills sub-bar on /discover", async () => {
    await renderTopBarAt("/discover");
    expect(screen.getByTestId("edge-filter-pills-sentinel")).toBeInTheDocument();
  });

  it("renders the filter pills sub-bar on /discover/<focal-node>", async () => {
    await renderTopBarAt("/discover?focus=person:foo");
    expect(screen.getByTestId("edge-filter-pills-sentinel")).toBeInTheDocument();
  });

  it("renders the filter pills sub-bar on /org/:companyId", async () => {
    await renderTopBarAt("/org/00000000-0000-0000-0000-000000000001");
    expect(screen.getByTestId("edge-filter-pills-sentinel")).toBeInTheDocument();
  });

  it("does NOT render the filter pills sub-bar on /validate", async () => {
    await renderTopBarAt("/validate");
    expect(screen.queryByTestId("edge-filter-pills-sentinel")).toBeNull();
  });

  it("does NOT render the filter pills sub-bar on /settings", async () => {
    await renderTopBarAt("/settings");
    expect(screen.queryByTestId("edge-filter-pills-sentinel")).toBeNull();
  });

  it("does NOT render the filter pills sub-bar on /prospect/:id", async () => {
    await renderTopBarAt("/prospect/abc");
    expect(screen.queryByTestId("edge-filter-pills-sentinel")).toBeNull();
  });

  it("does NOT render the filter pills sub-bar on /companies", async () => {
    await renderTopBarAt("/companies");
    expect(screen.queryByTestId("edge-filter-pills-sentinel")).toBeNull();
  });

  it("does NOT render the filter pills sub-bar on /people", async () => {
    await renderTopBarAt("/people");
    expect(screen.queryByTestId("edge-filter-pills-sentinel")).toBeNull();
  });

  it("does NOT render the filter pills sub-bar on the landing page", async () => {
    await renderTopBarAt("/");
    expect(screen.queryByTestId("edge-filter-pills-sentinel")).toBeNull();
  });
});
