import { v } from "convex/values";
import { internalMutation, query } from "./_generated/server";

export const writeScore = internalMutation({
  args: {
    prospect_id: v.id("prospects"),
    authenticity_score: v.number(),
    authority_score: v.number(),
    warmth_score: v.number(),
    overall_score: v.number(),
    falsification_notes: v.array(v.string()),
  },
  handler: async (ctx, args) => {
    return await ctx.db.insert("scores", { ...args, computed_at: Date.now() });
  },
});

export const latestForProspect = query({
  args: { prospect_id: v.id("prospects") },
  handler: async (ctx, { prospect_id }) => {
    const all = await ctx.db
      .query("scores")
      .withIndex("by_prospect", (q) => q.eq("prospect_id", prospect_id))
      .order("desc")
      .take(1);
    return all[0] ?? null;
  },
});

export const listLatestForProspects = query({
  args: { prospect_ids: v.array(v.id("prospects")) },
  handler: async (ctx, { prospect_ids }) => {
    const out: Record<string, any> = {};
    for (const pid of prospect_ids) {
      const latest = await ctx.db
        .query("scores")
        .withIndex("by_prospect", (q) => q.eq("prospect_id", pid))
        .order("desc")
        .take(1);
      if (latest[0]) out[pid] = latest[0];
    }
    return out;
  },
});
