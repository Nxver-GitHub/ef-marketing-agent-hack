"use node";

import { v } from "convex/values";
import { action } from "./_generated/server";
import { api, internal } from "./_generated/api";
import { OVERALL_WEIGHTS } from "./constants";
import {
  fetchCompanyHiring,
  fetchConferenceMentions,
  fetchCrunchbase,
  fetchGitHubActivity,
  fetchLinkedInPosts,
  fetchLinkedInProfile,
  fetchMutualConnections,
  fetchUSPTOPatents,
  type NormalizedSignal,
} from "./services/dataSources";

const SOURCE_FETCHERS: Record<
  string,
  (p: { name: string; company: string; role: string; linkedin_url?: string }) => Promise<NormalizedSignal[]>
> = {
  linkedin_profile: (p) => fetchLinkedInProfile({ url: p.linkedin_url, name: p.name, company: p.company }),
  linkedin_posts: (p) => fetchLinkedInPosts({ name: p.name, company: p.company }),
  uspto: (p) => fetchUSPTOPatents({ name: p.name, company: p.company }),
  github: (p) => fetchGitHubActivity({ name: p.name }),
  conference: (p) => fetchConferenceMentions({ name: p.name }),
  company_hiring: (p) => fetchCompanyHiring({ company: p.company, role: p.role }),
  crunchbase: (p) => fetchCrunchbase({ name: p.name, company: p.company }),
  mutual_connections: (p) =>
    fetchMutualConnections({ target_linkedin_url: p.linkedin_url, name: p.name }),
};

const ALL_SOURCES = Object.keys(SOURCE_FETCHERS);

// Sigmoid-ish normalization to 0..100 from arbitrary numeric value.
function normalize(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return 0;
  // Map 0..50 → 0..100 with diminishing returns.
  return Math.max(0, Math.min(100, 100 * (1 - Math.exp(-n / 15))));
}

export const computeTrustScore = action({
  args: { prospect_id: v.id("prospects") },
  handler: async (ctx, { prospect_id }) => {
    const prospect = await ctx.runQuery(api.prospects.get, { id: prospect_id });
    if (!prospect) throw new Error("prospect not found");

    const run_id = await ctx.runMutation(internal.scoringRuns.create, { prospect_id });
    await ctx.runMutation(internal.scoringRuns.update, {
      run_id,
      patch: { status: "running", sources_attempted: ALL_SOURCES },
    });

    const succeeded: string[] = [];
    const errors: string[] = [];

    for (const source of ALL_SOURCES) {
      await ctx.runMutation(internal.scoringRuns.update, {
        run_id,
        patch: { current_source: source },
      });
      try {
        const signals = await SOURCE_FETCHERS[source]({
          name: prospect.name,
          company: prospect.company,
          role: prospect.role,
          linkedin_url: prospect.linkedin_url,
        });
        await ctx.runMutation(internal.signals.insertMany, {
          prospect_id,
          signals,
        });
        succeeded.push(source);
      } catch (e) {
        errors.push(`${source}: ${(e as Error).message}`);
      }
    }

    // Load weights + signals to compute scores
    const weightsRows = await ctx.runQuery(api.signalWeights.list, {});
    const weightsMap = new Map(
      weightsRows.map((w: any) => [
        w.signal_type,
        {
          authenticity: w.authenticity_weight,
          authority: w.authority_weight,
          warmth: w.warmth_weight,
        },
      ])
    );
    const allSignals = await ctx.runQuery(api.signals.listForProspect, { prospect_id });

    let aNum = 0, aDen = 0, athNum = 0, athDen = 0, wNum = 0, wDen = 0;
    for (const s of allSignals as any[]) {
      const w = weightsMap.get(s.signal_type);
      if (!w) continue;
      const norm = normalize(s.value);
      const conf = s.confidence ?? 1;
      const base = (s.weight ?? 1) * conf;
      aNum += norm * base * w.authenticity;
      aDen += base * w.authenticity;
      athNum += norm * base * w.authority;
      athDen += base * w.authority;
      wNum += norm * base * w.warmth;
      wDen += base * w.warmth;
    }
    const authenticity = aDen ? aNum / aDen : 0;
    const authority = athDen ? athNum / athDen : 0;
    const warmth = wDen ? wNum / wDen : 0;
    const overall =
      OVERALL_WEIGHTS.authenticity * authenticity +
      OVERALL_WEIGHTS.authority * authority +
      OVERALL_WEIGHTS.warmth * warmth;

    const round = (n: number) => Math.round(n * 10) / 10;
    const falsification_notes = buildFalsificationNotes({
      prospect,
      succeeded,
      authenticity,
      authority,
      warmth,
    });

    await ctx.runMutation(internal.scores.writeScore, {
      prospect_id,
      authenticity_score: round(authenticity),
      authority_score: round(authority),
      warmth_score: round(warmth),
      overall_score: round(overall),
      falsification_notes,
    });

    await ctx.runMutation(internal.scoringRuns.update, {
      run_id,
      patch: {
        status: errors.length === ALL_SOURCES.length ? "error" : "complete",
        sources_succeeded: succeeded,
        current_source: undefined,
        error_log: errors.join(" | ") || undefined,
        completed_at: Date.now(),
      },
    });

    return { run_id };
  },
});

function buildFalsificationNotes(args: {
  prospect: any;
  succeeded: string[];
  authenticity: number;
  authority: number;
  warmth: number;
}): string[] {
  const notes: string[] = [];
  if (args.succeeded.includes("linkedin_profile"))
    notes.push(
      "Authenticity assumes LinkedIn tenure is accurate — if the profile was edited in the last 60 days, re-verify."
    );
  if (args.authority > 70 && !args.succeeded.includes("uspto"))
    notes.push(
      "Authority is high without USPTO confirmation — re-run when patent data succeeds to avoid false positives."
    );
  if (args.warmth > 50 && !args.succeeded.includes("mutual_connections"))
    notes.push(
      "Warmth assumes mutual-connection data is fresh — invalid if the user's LinkedIn graph hasn't synced this week."
    );
  if (!args.succeeded.includes("crunchbase"))
    notes.push(
      "Role not cross-checked against Crunchbase — if the prospect changed jobs in the last 30 days, score may be stale."
    );
  return notes.slice(0, 4);
}
