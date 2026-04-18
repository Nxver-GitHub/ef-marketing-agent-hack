import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

/**
 * Credence schema.
 *
 * Design principle: source-agnostic. New data sources plug in by writing
 * `signals` rows with a new `source` string. Scoring reads weights from
 * `signal_weights` at runtime — never hardcode in functions.
 */
export default defineSchema({
  prospects: defineTable({
    name: v.string(),
    company: v.string(),
    role: v.string(),
    industry: v.string(),
    linkedin_url: v.optional(v.string()),
    created_at: v.number(),
    updated_at: v.number(),
  })
    .index("by_industry", ["industry"])
    .index("by_company", ["company"]),

  signals: defineTable({
    prospect_id: v.id("prospects"),
    source: v.string(), // "linkedin_profile" | "uspto" | "github" | ...
    signal_type: v.string(), // "tenure_years" | "patent_count" | ...
    value: v.union(v.number(), v.string(), v.any()),
    raw_data: v.any(),
    weight: v.number(), // per-signal override; default 1.0
    confidence: v.number(), // 0..1
    collected_at: v.number(),
  })
    .index("by_prospect", ["prospect_id"])
    .index("by_prospect_and_source", ["prospect_id", "source"]),

  scores: defineTable({
    prospect_id: v.id("prospects"),
    authenticity_score: v.number(),
    authority_score: v.number(),
    warmth_score: v.number(),
    overall_score: v.number(),
    falsification_notes: v.array(v.string()),
    computed_at: v.number(),
  }).index("by_prospect", ["prospect_id"]),

  signal_weights: defineTable({
    signal_type: v.string(),
    authenticity_weight: v.number(),
    authority_weight: v.number(),
    warmth_weight: v.number(),
  }).index("by_signal_type", ["signal_type"]),

  scoring_runs: defineTable({
    prospect_id: v.id("prospects"),
    status: v.union(
      v.literal("pending"),
      v.literal("running"),
      v.literal("complete"),
      v.literal("error")
    ),
    sources_attempted: v.array(v.string()),
    sources_succeeded: v.array(v.string()),
    current_source: v.optional(v.string()),
    error_log: v.optional(v.string()),
    started_at: v.number(),
    completed_at: v.optional(v.number()),
  }).index("by_prospect", ["prospect_id"]),
});
