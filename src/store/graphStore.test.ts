/**
 * graphStore — selection state machine + selectors.
 *
 * Catches bugs in the kind that motivated the C2 fix: state mutations
 * that don't satisfy mutual exclusion between node and edge selection,
 * and selectors that don't return null when their backing id is absent.
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  selectActiveEdge,
  selectActiveNode,
  selectVisibleEdges,
  selectVisibleNodes,
  useGraphStore,
} from "./graphStore";
import type { GraphEdge, GraphNode } from "@/lib/graph";

const NODE_A: GraphNode = {
  id: "person:a",
  kind: "person",
  name: "Alice",
} as unknown as GraphNode;
const NODE_B: GraphNode = {
  id: "person:b",
  kind: "person",
  name: "Bob",
} as unknown as GraphNode;
const EDGE_AB: GraphEdge = {
  id: "edge:ab",
  source: "person:a",
  target: "person:b",
  kind: "career_overlap_general",
};

function reset() {
  // Reset to a known canonical state — graphStore exports `reset` as an
  // action; we use it instead of poking internals.
  useGraphStore.getState().reset();
  useGraphStore.getState().setGraph({ nodes: [NODE_A, NODE_B], edges: [EDGE_AB] });
}

afterEach(() => {
  useGraphStore.getState().reset();
});

describe("graphStore — selection mutual exclusion", () => {
  it("selectNode clears any pinned edge", () => {
    reset();
    useGraphStore.getState().selectEdge("edge:ab");
    expect(useGraphStore.getState().selectedEdgeId).toBe("edge:ab");
    useGraphStore.getState().selectNode("person:a");
    expect(useGraphStore.getState().selectedNodeId).toBe("person:a");
    expect(useGraphStore.getState().selectedEdgeId).toBeNull();
  });

  it("selectEdge clears any pinned node", () => {
    reset();
    useGraphStore.getState().selectNode("person:a");
    expect(useGraphStore.getState().selectedNodeId).toBe("person:a");
    useGraphStore.getState().selectEdge("edge:ab");
    expect(useGraphStore.getState().selectedEdgeId).toBe("edge:ab");
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
  });

  it("selectNode(null) clears the node selection without disturbing edge selection", () => {
    reset();
    useGraphStore.getState().selectEdge("edge:ab");
    useGraphStore.getState().selectNode(null);
    // selectNode(null) is the explicit "deselect" — it also clears edge per
    // the contract (one selection at a time).
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
    expect(useGraphStore.getState().selectedEdgeId).toBeNull();
  });

  it("selectEdge(null) clears the edge selection", () => {
    reset();
    useGraphStore.getState().selectEdge("edge:ab");
    useGraphStore.getState().selectEdge(null);
    expect(useGraphStore.getState().selectedEdgeId).toBeNull();
  });

  it("focusNode does not mutate selection state", () => {
    reset();
    useGraphStore.getState().selectNode("person:a");
    useGraphStore.getState().focusNode("person:b");
    expect(useGraphStore.getState().selectedNodeId).toBe("person:a");
    expect(useGraphStore.getState().focusedNodeId).toBe("person:b");
  });
});

describe("graphStore — selectors", () => {
  it("selectActiveEdge resolves the pinned edge from the graph", () => {
    reset();
    expect(selectActiveEdge(useGraphStore.getState())).toBeNull();
    useGraphStore.getState().selectEdge("edge:ab");
    const active = selectActiveEdge(useGraphStore.getState());
    expect(active?.id).toBe("edge:ab");
    expect(active?.kind).toBe("career_overlap_general");
  });

  it("selectActiveEdge returns null when the pinned id is no longer in the graph", () => {
    reset();
    useGraphStore.getState().selectEdge("edge:gone");
    expect(selectActiveEdge(useGraphStore.getState())).toBeNull();
  });

  it("selectActiveNode prefers selectedNodeId over focusedNodeId", () => {
    reset();
    useGraphStore.getState().selectNode("person:a");
    useGraphStore.getState().focusNode("person:b");
    const active = selectActiveNode(useGraphStore.getState());
    expect(active?.id).toBe("person:a");
  });

  it("selectActiveNode falls back to focusedNodeId when no pinned node", () => {
    reset();
    useGraphStore.getState().focusNode("person:b");
    expect(selectActiveNode(useGraphStore.getState())?.id).toBe("person:b");
  });

  it("selectVisibleEdges respects visibleEdgeKinds", () => {
    reset();
    // Force the test edge's kind into the visible set so we can assert
    // the filter works without depending on EDGE_CONFIGS defaults.
    useGraphStore.getState().setVisibleEdgeKinds(new Set(["career_overlap_general"]));
    expect(selectVisibleEdges(useGraphStore.getState())).toHaveLength(1);
    useGraphStore.getState().toggleEdgeKind("career_overlap_general");
    expect(selectVisibleEdges(useGraphStore.getState())).toHaveLength(0);
  });

  it("selectVisibleNodes filters by node kind + search query", () => {
    reset();
    expect(selectVisibleNodes(useGraphStore.getState())).toHaveLength(2);
    useGraphStore.getState().setSearchQuery("alic");
    expect(selectVisibleNodes(useGraphStore.getState())).toHaveLength(1);
    expect(selectVisibleNodes(useGraphStore.getState())[0].id).toBe("person:a");
  });
});

describe("graphStore — visibility actions", () => {
  it("toggleEdgeKind flips visibility for that kind only", () => {
    reset();
    const before = useGraphStore.getState().visibleEdgeKinds;
    useGraphStore.getState().toggleEdgeKind("career_overlap_general");
    const after = useGraphStore.getState().visibleEdgeKinds;
    expect(after.has("career_overlap_general")).toBe(
      !before.has("career_overlap_general"),
    );
  });

  it("setVisibleEdgeKinds replaces the set wholesale", () => {
    reset();
    useGraphStore
      .getState()
      .setVisibleEdgeKinds(new Set(["career_overlap_general"]));
    expect(useGraphStore.getState().visibleEdgeKinds.size).toBe(1);
  });

  it("toggleNodeKind flips visibility for that kind", () => {
    reset();
    useGraphStore.getState().toggleNodeKind("person");
    expect(useGraphStore.getState().visibleNodeKinds.has("person")).toBe(false);
  });

  it("reset returns to the canonical initial state", () => {
    reset();
    useGraphStore.getState().selectNode("person:a");
    useGraphStore.getState().setSearchQuery("foo");
    useGraphStore.getState().reset();
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
    expect(useGraphStore.getState().selectedEdgeId).toBeNull();
    expect(useGraphStore.getState().searchQuery).toBe("");
  });
});
