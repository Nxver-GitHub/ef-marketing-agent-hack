/**
 * Tests for warmPathsForCompany — pure aggregator coverage.
 */
import { describe, it, expect } from "vitest"
import type { WarmPath } from "./warmPaths"
import {
  aggregateWarmPathsByCompany,
  medianHopCount,
} from "./warmPathsForCompany"

// ── medianHopCount ──────────────────────────────────────────────────────────

describe("medianHopCount", () => {
  it("returns 0 for empty array", () => {
    expect(medianHopCount([])).toBe(0)
  })

  it("returns the single value for length 1", () => {
    expect(medianHopCount([3])).toBe(3)
  })

  it("returns middle value for odd-length sorted input", () => {
    expect(medianHopCount([1, 2, 3, 4, 5])).toBe(3)
  })

  it("returns rounded average of middle pair for even-length input", () => {
    expect(medianHopCount([1, 2, 3, 4])).toBe(3) // round(2.5) = 3 (banker)
    // Verify the rounding direction with a clearer pair.
    expect(medianHopCount([2, 4])).toBe(3) // round(3.0) = 3
    expect(medianHopCount([2, 5])).toBe(4) // round(3.5) = 4
  })

  it("sorts unsorted input before computing median", () => {
    expect(medianHopCount([5, 1, 3, 2, 4])).toBe(3)
  })

  it("does not mutate the input array", () => {
    const input = [3, 1, 2]
    medianHopCount(input)
    expect(input).toEqual([3, 1, 2])
  })
})

// ── aggregateWarmPathsByCompany ────────────────────────────────────────────

function makePath(
  terminalId: string,
  strength: number,
  hopCount: number,
): WarmPath {
  // Minimal valid WarmPath — only the fields the aggregator reads matter.
  return {
    nodes: [
      { id: "src", kind: "person", label: "Source" },
      { id: terminalId, kind: "person", label: terminalId },
    ] as WarmPath["nodes"],
    edges: [] as WarmPath["edges"],
    strength,
    hopCount,
    explanation: "test path",
    suggested_opener: "test opener",
  }
}

describe("aggregateWarmPathsByCompany", () => {
  it("returns empty map for empty input", () => {
    const out = aggregateWarmPathsByCompany([], new Map())
    expect(out.size).toBe(0)
  })

  it("groups paths by terminal company", () => {
    const personToCompany = new Map<string, string>([
      ["alice", "intel"],
      ["bob", "intel"],
      ["charlie", "nvidia"],
    ])
    const paths = [
      makePath("alice", 0.8, 1),
      makePath("bob", 0.6, 2),
      makePath("charlie", 0.9, 1),
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.size).toBe(2)
    expect(out.get("intel")?.total_paths).toBe(2)
    expect(out.get("nvidia")?.total_paths).toBe(1)
  })

  it("counts unique_targets per company", () => {
    const personToCompany = new Map<string, string>([
      ["alice", "intel"],
      ["bob", "intel"],
    ])
    const paths = [
      makePath("alice", 0.5, 1),
      makePath("alice", 0.7, 2), // same target alice → +1 path, no new target
      makePath("bob", 0.6, 1),
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.get("intel")?.total_paths).toBe(3)
    expect(out.get("intel")?.unique_targets).toBe(2)
  })

  it("tracks best_strength as the maximum across all paths to that co", () => {
    const personToCompany = new Map<string, string>([["alice", "intel"]])
    const paths = [
      makePath("alice", 0.5, 1),
      makePath("alice", 0.95, 2),
      makePath("alice", 0.6, 1),
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.get("intel")?.best_strength).toBe(0.95)
  })

  it("computes median_hop_count over all paths to the company", () => {
    const personToCompany = new Map<string, string>([
      ["alice", "intel"],
      ["bob", "intel"],
      ["charlie", "intel"],
    ])
    const paths = [
      makePath("alice", 0.5, 1),
      makePath("bob", 0.5, 3),
      makePath("charlie", 0.5, 5),
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.get("intel")?.median_hop_count).toBe(3) // median of [1,3,5]
  })

  it("rounds even-length medians", () => {
    const personToCompany = new Map<string, string>([
      ["a", "co"],
      ["b", "co"],
    ])
    const paths = [makePath("a", 0.5, 2), makePath("b", 0.5, 4)]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.get("co")?.median_hop_count).toBe(3) // round(3.0) = 3
  })

  it("per_target.bestStrength tracks max strength per target", () => {
    const personToCompany = new Map<string, string>([["alice", "intel"]])
    const paths = [
      makePath("alice", 0.5, 1),
      makePath("alice", 0.9, 3),
      makePath("alice", 0.7, 2),
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.get("intel")?.per_target.get("alice")?.bestStrength).toBe(0.9)
    expect(out.get("intel")?.per_target.get("alice")?.bestHops).toBe(3)
  })

  it("ties on strength break to lower hopCount", () => {
    const personToCompany = new Map<string, string>([["alice", "intel"]])
    const paths = [
      makePath("alice", 0.8, 3), // first hits, becomes bestStrength
      makePath("alice", 0.8, 1), // ties strength, lower hops → bestHops updates
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    const t = out.get("intel")?.per_target.get("alice")
    expect(t?.bestStrength).toBe(0.8)
    expect(t?.bestHops).toBe(1)
  })

  it("silently drops paths whose terminal isn't in personIdToCompanyId", () => {
    const personToCompany = new Map<string, string>([["alice", "intel"]])
    const paths = [
      makePath("alice", 0.5, 1),
      makePath("ghost", 0.9, 1), // ghost not in the map → dropped
    ]
    const out = aggregateWarmPathsByCompany(paths, personToCompany)
    expect(out.size).toBe(1)
    expect(out.get("intel")?.total_paths).toBe(1)
  })

  it("skips malformed paths without nodes", () => {
    const personToCompany = new Map<string, string>([["alice", "intel"]])
    const broken = {
      nodes: [],
      edges: [],
      strength: 0.5,
      hopCount: 1,
      explanation: "",
      suggested_opener: "",
    } as unknown as WarmPath
    const out = aggregateWarmPathsByCompany([broken], personToCompany)
    expect(out.size).toBe(0)
  })
})
