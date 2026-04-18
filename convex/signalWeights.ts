import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { DEFAULT_SIGNAL_WEIGHTS } from "./constants";

export const list = query({
  args: {},
  handler: async (ctx) => ctx.db.query("signal_weights").collect(),
});

export const upsert = mutation({
  args: {
    signal_type: v.string(),
    authenticity_weight: v.number(),
    authority_weight: v.number(),
    warmth_weight: v.number(),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("signal_weights")
      .withIndex("by_signal_type", (q) => q.eq("signal_type", args.signal_type))
      .unique();
    if (existing) {
      await ctx.db.patch(existing._id, args);
      return existing._id;
    }
    return await ctx.db.insert("signal_weights", args);
  },
});

export const seedDefaults = mutation({
  args: {},
  handler: async (ctx) => {
    const existing = await ctx.db.query("signal_weights").collect();
    const have = new Set(existing.map((r) => r.signal_type));
    for (const [signal_type, w] of Object.entries(DEFAULT_SIGNAL_WEIGHTS)) {
      if (have.has(signal_type)) continue;
      await ctx.db.insert("signal_weights", {
        signal_type,
        authenticity_weight: w.authenticity,
        authority_weight: w.authority,
        warmth_weight: w.warmth,
      });
    }
  },
});
