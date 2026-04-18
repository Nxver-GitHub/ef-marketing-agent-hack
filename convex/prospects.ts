import { v } from "convex/values";
import { mutation, query } from "./_generated/server";

export const create = mutation({
  args: {
    name: v.string(),
    company: v.string(),
    role: v.string(),
    industry: v.string(),
    linkedin_url: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const now = Date.now();
    return await ctx.db.insert("prospects", { ...args, created_at: now, updated_at: now });
  },
});

export const get = query({
  args: { id: v.id("prospects") },
  handler: async (ctx, { id }) => ctx.db.get(id),
});

export const listByIndustry = query({
  args: { industry: v.string() },
  handler: async (ctx, { industry }) =>
    ctx.db
      .query("prospects")
      .withIndex("by_industry", (q) => q.eq("industry", industry))
      .collect(),
});

export const listAll = query({
  args: {},
  handler: async (ctx) => ctx.db.query("prospects").order("desc").take(100),
});
