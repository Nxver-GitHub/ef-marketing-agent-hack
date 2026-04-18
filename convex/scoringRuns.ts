import { v } from "convex/values";
import { internalMutation, query } from "./_generated/server";

export const create = internalMutation({
  args: { prospect_id: v.id("prospects") },
  handler: async (ctx, { prospect_id }) => {
    return await ctx.db.insert("scoring_runs", {
      prospect_id,
      status: "pending",
      sources_attempted: [],
      sources_succeeded: [],
      started_at: Date.now(),
    });
  },
});

export const update = internalMutation({
  args: {
    run_id: v.id("scoring_runs"),
    patch: v.object({
      status: v.optional(
        v.union(
          v.literal("pending"),
          v.literal("running"),
          v.literal("complete"),
          v.literal("error")
        )
      ),
      sources_attempted: v.optional(v.array(v.string())),
      sources_succeeded: v.optional(v.array(v.string())),
      current_source: v.optional(v.string()),
      error_log: v.optional(v.string()),
      completed_at: v.optional(v.number()),
    }),
  },
  handler: async (ctx, { run_id, patch }) => {
    await ctx.db.patch(run_id, patch);
  },
});

export const latestForProspect = query({
  args: { prospect_id: v.id("prospects") },
  handler: async (ctx, { prospect_id }) => {
    const r = await ctx.db
      .query("scoring_runs")
      .withIndex("by_prospect", (q) => q.eq("prospect_id", prospect_id))
      .order("desc")
      .take(1);
    return r[0] ?? null;
  },
});
