import { v } from "convex/values";
import { internalMutation, mutation, query } from "./_generated/server";

export const insertMany = internalMutation({
  args: {
    prospect_id: v.id("prospects"),
    signals: v.array(
      v.object({
        source: v.string(),
        signal_type: v.string(),
        value: v.any(),
        raw_data: v.any(),
        weight: v.number(),
        confidence: v.number(),
      })
    ),
  },
  handler: async (ctx, { prospect_id, signals }) => {
    const now = Date.now();
    for (const s of signals) {
      await ctx.db.insert("signals", { ...s, prospect_id, collected_at: now });
    }
  },
});

export const insertOne = mutation({
  args: {
    prospect_id: v.id("prospects"),
    source: v.string(),
    signal_type: v.string(),
    value: v.any(),
    raw_data: v.any(),
    weight: v.number(),
    confidence: v.number(),
  },
  handler: async (ctx, args) => {
    return await ctx.db.insert("signals", { ...args, collected_at: Date.now() });
  },
});

export const listForProspect = query({
  args: { prospect_id: v.id("prospects") },
  handler: async (ctx, { prospect_id }) =>
    ctx.db
      .query("signals")
      .withIndex("by_prospect", (q) => q.eq("prospect_id", prospect_id))
      .collect(),
});
