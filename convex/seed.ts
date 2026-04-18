import { v } from "convex/values";
import { action } from "./_generated/server";
import { api } from "./_generated/api";

/**
 * Seed a few example prospects for the /discover demo.
 */
const SEED = [
  { name: "Lin Wei", company: "TSMC", role: "VP Process Engineering", industry: "Semiconductors" },
  { name: "Ana Souza", company: "ASML", role: "Director Lithography", industry: "Semiconductors" },
  { name: "Marcus Hale", company: "Intel", role: "Principal Engineer", industry: "Semiconductors" },
  { name: "Priya Raman", company: "NVIDIA", role: "Director of HW", industry: "Semiconductors" },
  { name: "Jonas Berg", company: "Infineon", role: "Head of Power", industry: "Semiconductors" },
];

export const seedDemoData = action({
  args: {},
  handler: async (ctx) => {
    await ctx.runMutation(api.signalWeights.seedDefaults, {});
    const existing = await ctx.runQuery(api.prospects.listAll, {});
    if ((existing as any[]).length > 0) return { skipped: true };
    for (const p of SEED) {
      const id = await ctx.runMutation(api.prospects.create, p);
      await ctx.runAction(api.scoring.computeTrustScore, { prospect_id: id });
    }
    return { seeded: SEED.length };
  },
});
