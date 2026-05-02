/**
 * Adapter: GraphEdge (canvas shape) → EdgeInspectorEdge (inspector shape).
 *
 * Extracted from Discover.tsx so the source-type inference, person-node
 * resolution, and base-strength lookup are testable in isolation. The
 * canvas only carries minimal edge metadata (id, source, target, kind,
 * optional evidence singleton); the inspector wants strength factors and
 * a resolved-person pair on each side.
 *
 * If a future loader populates `recency_factor` / `frequency_factor` /
 * `corroboration_factor` on `GraphEdge`, the adapter will surface them —
 * the current shape simply forwards `null` for absent data per Contract 1
 * "tolerate missing fields" semantics.
 */
import type { GraphEdge, GraphNode } from "@/lib/graph";
import type {
  EdgeEvidence as InspectorEdgeEvidence,
  EdgeInspectorEdge,
} from "@/components/EdgeInspector";
import { STRENGTH_TABLE } from "@/lib/strength";

export type SourceType = InspectorEdgeEvidence["source_type"];

/**
 * Map a GraphEdge.kind (loosely-typed string) to the inspector's
 * source_type discriminator. Order-dependent: more-specific prefixes
 * must come before more-general ones (academic → paper, before any
 * generic match).
 */
export function inferSourceType(kind: string): SourceType {
  if (kind.startsWith("patent")) return "patent";
  if (kind.startsWith("academic")) return "paper";
  if (kind.startsWith("career_overlap")) return "career_overlap";
  if (kind.startsWith("standards")) return "standards";
  if (kind.startsWith("conference")) return "conference";
  if (
    kind.includes("cohort") ||
    kind.includes("alumni") ||
    kind === "executive_education" ||
    // Education-cohort kinds use the "same_*" naming convention
    // (same_mba_cohort, same_phd_program, same_undergrad_cohort, etc.)
    // even when the suffix isn't literally "cohort".
    kind.startsWith("same_")
  ) {
    return "cohort";
  }
  return "unknown";
}

/**
 * Resolve the source/target node id from a GraphEdge regardless of
 * whether ForceGraph2D has hydrated `source`/`target` to objects (post-
 * mount) or kept them as string ids (pre-tick). The cohort of helpers
 * inside Discover.tsx all repeat this dance — centralising here.
 */
export function resolveEndpointId(
  endpoint: GraphEdge["source"] | GraphEdge["target"],
): string {
  return typeof endpoint === "string"
    ? endpoint
    : (endpoint as { id: string }).id;
}

interface PersonLookup {
  id: string;
  name?: string;
  role?: string | null;
  company?: string | null;
}

function resolvePerson(
  id: string,
  nodes: ReadonlyArray<GraphNode>,
): EdgeInspectorEdge["source_person"] {
  const node = nodes.find((n) => n.id === id);
  // GraphNode has a name; person nodes additionally carry role/company
  // (this lookup is shape-tolerant — non-person hub nodes resolve with
  // null role/company instead of throwing).
  const person = node as unknown as PersonLookup | undefined;
  return {
    id,
    canonical_name: person?.name ?? id,
    current_title: person?.role ?? null,
    current_company_name: person?.company ?? null,
  };
}

/**
 * Build the EdgeInspectorEdge payload a render call expects.
 *
 * Returns `null` when `edge` is null/undefined. The hosting component
 * uses that to switch back to NodeInspector.
 */
export function adaptGraphEdgeForInspector(
  edge: GraphEdge | null | undefined,
  nodes: ReadonlyArray<GraphNode>,
): EdgeInspectorEdge | null {
  if (!edge) return null;
  const sourceId = resolveEndpointId(edge.source);
  const targetId = resolveEndpointId(edge.target);
  const baseStrength = STRENGTH_TABLE[edge.kind] ?? 0.5;
  const evidence: InspectorEdgeEvidence[] = edge.evidence
    ? [
        {
          source_type: inferSourceType(edge.kind),
          structured_value: edge.evidence as unknown as Record<string, unknown>,
        },
      ]
    : [];
  return {
    id: edge.id,
    connection_type: edge.kind,
    base_strength: baseStrength,
    recency_factor: null,
    frequency_factor: null,
    corroboration_factor: null,
    computed_strength: baseStrength,
    evidence,
    source_person: resolvePerson(sourceId, nodes),
    target_person: resolvePerson(targetId, nodes),
  };
}
