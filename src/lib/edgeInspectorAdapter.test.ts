import { describe, expect, it } from "vitest";
import {
  adaptGraphEdgeForInspector,
  inferSourceType,
  resolveEndpointId,
} from "./edgeInspectorAdapter";
import type { GraphEdge, GraphNode } from "@/lib/graph";

const PERSON_A: GraphNode = {
  id: "person:a",
  kind: "person",
  name: "Alice Anderson",
  role: "Director",
  company: "Acme",
} as unknown as GraphNode;
const PERSON_B: GraphNode = {
  id: "person:b",
  kind: "person",
  name: "Bob Builder",
  role: "VP",
  company: "Beacon",
} as unknown as GraphNode;
const NODES = [PERSON_A, PERSON_B];

describe("inferSourceType", () => {
  it.each([
    ["patent_co_inventor", "patent"],
    ["academic_co_author_multi", "paper"],
    ["academic_co_author_single", "paper"],
    ["career_overlap_general", "career_overlap"],
    ["career_overlap_same_team", "career_overlap"],
    ["career_overlap_same_domain", "career_overlap"],
    ["standards_committee_peer", "standards"],
    ["conference_co_presenter", "conference"],
    ["conference_co_attendee", "conference"],
    ["same_undergrad_cohort", "cohort"],
    ["same_mba_cohort", "cohort"],
    ["same_phd_program", "cohort"],
    ["alumni_network", "cohort"],
    ["executive_education", "cohort"],
    ["works_at", "unknown"],
    ["located_in", "unknown"],
    ["", "unknown"],
  ])("maps %s → %s", (kind, expected) => {
    expect(inferSourceType(kind)).toBe(expected);
  });
});

describe("resolveEndpointId", () => {
  it("returns string ids unchanged", () => {
    expect(resolveEndpointId("person:foo")).toBe("person:foo");
  });
  it("extracts .id from hydrated objects (post-tick ForceGraph2D)", () => {
    expect(resolveEndpointId({ id: "person:bar" } as unknown as GraphEdge["source"])).toBe(
      "person:bar",
    );
  });
});

describe("adaptGraphEdgeForInspector", () => {
  const baseEdge: GraphEdge = {
    id: "edge:1",
    source: "person:a",
    target: "person:b",
    kind: "academic_co_author_multi",
  } as GraphEdge;

  it("returns null for a null/undefined edge", () => {
    expect(adaptGraphEdgeForInspector(null, NODES)).toBeNull();
    expect(adaptGraphEdgeForInspector(undefined, NODES)).toBeNull();
  });

  it("resolves source and target persons from the nodes array", () => {
    const result = adaptGraphEdgeForInspector(baseEdge, NODES);
    expect(result).not.toBeNull();
    expect(result?.source_person).toMatchObject({
      id: "person:a",
      canonical_name: "Alice Anderson",
      current_title: "Director",
      current_company_name: "Acme",
    });
    expect(result?.target_person).toMatchObject({
      id: "person:b",
      canonical_name: "Bob Builder",
    });
  });

  it("falls back to id-as-name when a node is missing from the lookup", () => {
    const result = adaptGraphEdgeForInspector(
      { ...baseEdge, source: "person:ghost" },
      NODES,
    );
    expect(result?.source_person.canonical_name).toBe("person:ghost");
    expect(result?.source_person.current_title).toBeNull();
  });

  it("populates connection_type and computes a non-zero base_strength", () => {
    const result = adaptGraphEdgeForInspector(baseEdge, NODES);
    expect(result?.connection_type).toBe("academic_co_author_multi");
    expect(result?.base_strength).toBeGreaterThan(0);
    expect(result?.computed_strength).toBeGreaterThan(0);
  });

  it("emits an empty evidence array when GraphEdge.evidence is missing", () => {
    const result = adaptGraphEdgeForInspector(baseEdge, NODES);
    expect(result?.evidence).toEqual([]);
  });

  it("wraps GraphEdge.evidence singleton into an array with the inferred source_type", () => {
    const result = adaptGraphEdgeForInspector(
      {
        ...baseEdge,
        kind: "patent_co_inventor",
        evidence: { kind: "patent_co_inventor", patent_number: "US10234567" } as unknown as GraphEdge["evidence"],
      },
      NODES,
    );
    expect(result?.evidence).toHaveLength(1);
    expect(result?.evidence[0].source_type).toBe("patent");
    expect(result?.evidence[0].structured_value).toMatchObject({
      patent_number: "US10234567",
    });
  });

  it("handles ForceGraph-hydrated source/target objects", () => {
    const hydrated = {
      ...baseEdge,
      source: { id: "person:a" } as unknown as GraphEdge["source"],
      target: { id: "person:b" } as unknown as GraphEdge["target"],
    };
    const result = adaptGraphEdgeForInspector(hydrated, NODES);
    expect(result?.source_person.id).toBe("person:a");
    expect(result?.target_person.id).toBe("person:b");
  });

  it("never returns NaN/undefined factors — uses null sentinels for absent data", () => {
    const result = adaptGraphEdgeForInspector(baseEdge, NODES);
    expect(result?.recency_factor).toBeNull();
    expect(result?.frequency_factor).toBeNull();
    expect(result?.corroboration_factor).toBeNull();
  });

  it("returns base_strength=0.5 fallback for unknown edge kinds", () => {
    const result = adaptGraphEdgeForInspector(
      { ...baseEdge, kind: "made_up_kind" as GraphEdge["kind"] },
      NODES,
    );
    expect(result?.base_strength).toBe(0.5);
  });
});
