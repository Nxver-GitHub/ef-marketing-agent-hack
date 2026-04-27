/**
 * Helpers for "which prospects are attached to a given aggregation node?".
 *
 * Aggregation nodes are anything non-person — company / industry / role / city
 * / school / conference. The graph builder in `graph.ts` derives those nodes
 * from prospect fields (current+past company, industry, education, talks, …).
 * Going the other way (given an aggregation id, what prospects power it?)
 * is needed both:
 *   - to expand the rendered set when the user focuses on a hub (Discover.tsx)
 *   - to show truthful counts in the inspector right rail (NodeInspector.tsx)
 *
 * Centralising the logic keeps those two consumers honest with each other —
 * if Discover says "428 people connected to Micron", the inspector should
 * agree.
 */
import {
  canonicalizeRole,
  normalizeCompany,
  normalizeKey,
  resolveCompanyMeta,
} from "@/lib/graph";

export interface AggregationProspect {
  _id: string;
  name?: string;
  company: string;
  role: string;
  industry?: string;
  past_companies?: string[];
  education?: { school?: string }[];
  talks?: { venue?: string; year?: number }[];
}

/**
 * Return the IDs (`prospect._id`, no `person:` prefix) of every prospect
 * whose buildGraph footprint would attach to the given aggregation node.
 * Returns `null` when the focusId is null / a person / unsupported kind.
 */
export function prospectIdsForAggregation(
  focusId: string | null | undefined,
  prospects: readonly AggregationProspect[],
): Set<string> | null {
  if (!focusId) return null;
  const idx = focusId.indexOf(":");
  if (idx < 0) return null;
  const kind = focusId.slice(0, idx);
  const key = focusId.slice(idx + 1);
  if (kind === "person") return null;

  const out = new Set<string>();
  if (kind === "company") {
    for (const p of prospects) {
      if (normalizeCompany(p.company) === key) {
        out.add(p._id);
        continue;
      }
      if (p.past_companies?.some((c) => normalizeCompany(c) === key)) out.add(p._id);
    }
  } else if (kind === "industry") {
    for (const p of prospects) {
      const direct = (p.industry ?? "").trim();
      if (direct && normalizeKey(direct) === key) {
        out.add(p._id);
        continue;
      }
      const meta = resolveCompanyMeta(p.company);
      if (meta?.industry && normalizeKey(meta.industry) === key) out.add(p._id);
    }
  } else if (kind === "school") {
    for (const p of prospects) {
      if (p.education?.some((e) => e.school && normalizeKey(e.school) === key)) {
        out.add(p._id);
      }
    }
  } else if (kind === "role") {
    for (const p of prospects) {
      if (!p.role) continue;
      const canonical = canonicalizeRole(p.role);
      if (canonical && normalizeKey(canonical) === key) out.add(p._id);
    }
  } else if (kind === "conference") {
    for (const p of prospects) {
      const talks = p.talks;
      if (!talks) continue;
      for (const t of talks) {
        if (!t.venue) continue;
        const label = t.year ? `${t.venue} ${t.year}` : t.venue;
        if (normalizeKey(label) === key) {
          out.add(p._id);
          break;
        }
      }
    }
  } else if (kind === "city") {
    for (const p of prospects) {
      const meta = resolveCompanyMeta(p.company);
      if (!meta) continue;
      const cityName = meta.state ?? meta.country;
      if (cityName && normalizeKey(cityName) === key) out.add(p._id);
    }
  }

  return out.size > 0 ? out : null;
}

export interface HubStats {
  /** Count of prospects connected to this hub (across the full population). */
  total: number;
  /** Average overall score across those prospects (0 if none scored). */
  avgScore: number;
  /** How many of them are "high-confidence" (overall ≥ 75). */
  highConf: number;
  /** Top-3 roles by count for the people in this hub (canonicalized labels). */
  topRoles: { label: string; count: number }[];
  /** Top-3 industries — only meaningful for company / city / school nodes. */
  topIndustries: { label: string; count: number }[];
  /** Up to 8 best-scoring representatives, for click-through. */
  topPeople: { id: string; name: string; role: string; score: number }[];
}

/** Compute summary stats for an aggregation hub from the live data. */
export function computeHubStats(
  prospectIds: Set<string>,
  prospects: readonly AggregationProspect[],
  scores: Record<string, { overall_score?: number } | undefined>,
): HubStats {
  const members: AggregationProspect[] = [];
  for (const p of prospects) if (prospectIds.has(p._id)) members.push(p);

  const roleTally = new Map<string, number>();
  const indTally = new Map<string, number>();
  let scoreSum = 0;
  let scoreN = 0;
  let highConf = 0;
  const ranked: { id: string; name: string; role: string; score: number }[] = [];

  for (const p of members) {
    const role = p.role ? canonicalizeRole(p.role) : "";
    if (role) roleTally.set(role, (roleTally.get(role) ?? 0) + 1);
    const industry =
      (p.industry?.trim() || resolveCompanyMeta(p.company)?.industry || "").trim();
    if (industry) indTally.set(industry, (indTally.get(industry) ?? 0) + 1);

    const overall = scores[p._id]?.overall_score;
    if (typeof overall === "number") {
      scoreSum += overall;
      scoreN += 1;
      if (overall >= 75) highConf += 1;
    }
    ranked.push({
      id: p._id,
      name: p.name ?? p.role ?? p._id.slice(0, 6),
      role: p.role ?? "",
      score: overall ?? 0,
    });
  }

  ranked.sort((a, b) => b.score - a.score);
  const topByCount = (m: Map<string, number>) =>
    [...m.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([label, count]) => ({ label, count }));

  return {
    total: members.length,
    avgScore: scoreN > 0 ? Math.round((scoreSum / scoreN) * 10) / 10 : 0,
    highConf,
    topRoles: topByCount(roleTally),
    topIndustries: topByCount(indTally),
    topPeople: ranked.slice(0, 8),
  };
}
