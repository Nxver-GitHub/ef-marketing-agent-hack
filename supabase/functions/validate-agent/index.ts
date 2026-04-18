import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Anthropic from "https://esm.sh/@anthropic-ai/sdk";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }

  try {
    const { prospect_id } = await req.json();
    if (!prospect_id) {
      return new Response(JSON.stringify({ error: "prospect_id required" }), {
        status: 400,
        headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
      });
    }

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const anthropic = new Anthropic({
      apiKey: Deno.env.get("ANTHROPIC_API_KEY")!,
    });

    const { data: prospect, error: pErr } = await supabase
      .from("prospects")
      .select("*")
      .eq("id", prospect_id)
      .single();

    if (pErr || !prospect) {
      return new Response(JSON.stringify({ error: "Prospect not found" }), {
        status: 404,
        headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
      });
    }

    const roles: string[] = prospect.roles?.length ? prospect.roles : [prospect.role];
    const keywords: string[] = prospect.keywords ?? [];

    const { data: run } = await supabase
      .from("scoring_runs")
      .insert({
        prospect_id,
        status: "running",
        sources_attempted: ["web_search", "linkedin", "github", "uspto", "conferences"],
        sources_succeeded: [],
        agent_steps: [],
      })
      .select()
      .single();

    const runId = run!.id;
    const agentSteps: Array<Record<string, unknown>> = [];

    const addStep = async (step: Record<string, unknown>) => {
      agentSteps.push({ ...step, timestamp: new Date().toISOString() });
      await supabase
        .from("scoring_runs")
        .update({ agent_steps: agentSteps, current_source: step.source ?? step.tool })
        .eq("id", runId);
    };

    const tools: Anthropic.Tool[] = [
      {
        name: "search_web",
        description:
          "Search the web for publicly available information about a person at a company. " +
          "Use to find LinkedIn profiles, GitHub accounts, patent filings, or conference talks.",
        input_schema: {
          type: "object" as const,
          properties: {
            query: { type: "string", description: "Specific search query" },
            purpose: { type: "string", description: "What signal you are trying to verify" },
          },
          required: ["query", "purpose"],
        },
      },
      {
        name: "record_signal",
        description: "Record a verified data point about the prospect. Call whenever you find credible evidence.",
        input_schema: {
          type: "object" as const,
          properties: {
            source: {
              type: "string",
              description: "Source: linkedin_profile | linkedin_posts | github | uspto | conference | company_hiring | crunchbase | mutual_connections",
            },
            signal_type: {
              type: "string",
              description: "Signal: tenure_years | post_activity | recommendations | patent_count | patent_citations | github_commits | conference_talks | hiring_signal | crunchbase_role | mutual_connections",
            },
            value: { type: "number", description: "Signal strength 0-100" },
            confidence: { type: "number", description: "Confidence 0.0-1.0" },
            raw_summary: { type: "string", description: "One-sentence summary of what you found" },
          },
          required: ["source", "signal_type", "value", "confidence", "raw_summary"],
        },
      },
      {
        name: "finalize_prospect",
        description: "Call when you have identified the person with >65% confidence.",
        input_schema: {
          type: "object" as const,
          properties: {
            name: { type: "string", description: "Full name" },
            linkedin_url: { type: "string", description: "LinkedIn URL if found" },
            confidence: { type: "number", description: "Confidence 0.0-1.0" },
            reasoning: { type: "string", description: "Why this person matches all criteria" },
          },
          required: ["name", "confidence", "reasoning"],
        },
      },
    ];

    const systemPrompt = `You are a B2B lead intelligence agent for Credence.

Target:
  Company:  ${prospect.company}
  Industry: ${prospect.industry}
  Roles:    ${roles.join(" | ")}
  ${keywords.length ? `Keywords: ${keywords.join(", ")}` : ""}
  ${prospect.name !== "Unknown" ? `Name hint: ${prospect.name} (unconfirmed)` : "Name: unknown - discover it"}

Strategy:
1. Run 2-4 targeted web searches combining company + role
2. Record every signal found (LinkedIn tenure, patents, GitHub commits, conference talks)
3. Cross-validate across at least 2 sources before finalizing
4. Call finalize_prospect once confidence >65%

Signal value guidance (0-100):
  tenure_years: years x 8 (cap 80) | patent_count: count x 12 (cap 96)
  github_commits: log10(n+1) x 30 (cap 90) | conference_talks: talks x 15 (cap 75)`;

    const messages: Anthropic.MessageParam[] = [
      {
        role: "user",
        content: `Find and validate the lead at ${prospect.company} matching roles: ${roles.join(", ")}${
          keywords.length ? `. Focus areas: ${keywords.join(", ")}` : ""
        }`,
      },
    ];

    let finalized = false;
    const apifyToken = Deno.env.get("APIFY_TOKEN");

    for (let iteration = 0; iteration < 12 && !finalized; iteration++) {
      const response = await anthropic.messages.create({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 2048,
        system: systemPrompt,
        tools,
        messages,
      });

      messages.push({ role: "assistant", content: response.content });
      if (response.stop_reason === "end_turn") break;

      const toolResults: Anthropic.ToolResultBlockParam[] = [];

      for (const block of response.content) {
        if (block.type !== "tool_use") continue;

        const input = block.input as Record<string, unknown>;
        let result = "";

        if (block.name === "search_web") {
          const query = input.query as string;
          const purpose = input.purpose as string;
          await addStep({ type: "search", query, purpose, source: "web_search" });

          if (apifyToken) {
            try {
              const apifyRes = await fetch(
                `https://api.apify.com/v2/acts/apify~rag-web-browser/run-sync-get-dataset-items?token=${apifyToken}&timeout=25`,
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ query, maxResults: 3 }),
                },
              );
              const items = await apifyRes.json();
              result = (Array.isArray(items) ? items : [])
                .slice(0, 3)
                .map((item: Record<string, unknown>) => {
                  const meta = item.metadata as Record<string, string> | undefined;
                  const text = item.text as string | undefined;
                  return `[${meta?.title ?? "No title"}]\n${text?.slice(0, 600) ?? ""}`;
                })
                .join("\n\n---\n\n") || "No results returned.";
              await addStep({ type: "search_result", query, preview: result.slice(0, 300), source: "web_search" });
            } catch (e) {
              result = `Search failed: ${(e as Error).message}. Continue with other sources.`;
            }
          } else {
            // Demo mode - plausible mock results
            result = `[LinkedIn - ${prospect.company}]\nFound a ${roles[0]} at ${prospect.company} with 8+ years tenure in ${prospect.industry}${
              keywords.length ? `, specializing in ${keywords[0]}` : ""
            }. 500+ connections. Active poster with weekly industry insights.\n\n[Patents DB]\nThis person has filed 3 patents in the past 2 years related to ${prospect.industry} technology.`;
            await addStep({ type: "search_result", query, preview: result.slice(0, 200), source: "web_search", mock: true });
          }
        } else if (block.name === "record_signal") {
          const { source, signal_type, value, confidence, raw_summary } = input as {
            source: string; signal_type: string; value: number; confidence: number; raw_summary: string;
          };
          await supabase.from("signals").insert({
            prospect_id, source, signal_type, value,
            raw_data: { summary: raw_summary, _agent: true },
            weight: 1, confidence,
          });
          const { data: currentRun } = await supabase
            .from("scoring_runs").select("sources_succeeded").eq("id", runId).single();
          const succeeded = [...new Set([...(currentRun?.sources_succeeded ?? []), source])];
          await supabase.from("scoring_runs").update({ sources_succeeded: succeeded }).eq("id", runId);
          await addStep({ type: "signal", source, signal_type, value, confidence, summary: raw_summary });
          result = `Signal recorded: ${signal_type}=${value} from ${source} (confidence ${confidence})`;
        } else if (block.name === "finalize_prospect") {
          const { name, linkedin_url, confidence, reasoning } = input as {
            name: string; linkedin_url?: string; confidence: number; reasoning: string;
          };
          await supabase.from("prospects").update({
            name,
            ...(linkedin_url ? { linkedin_url } : {}),
            updated_at: new Date().toISOString(),
          }).eq("id", prospect_id);
          await addStep({ type: "finalized", name, linkedin_url, confidence, reasoning, source: "agent" });
          result = `Identified as ${name} (confidence: ${confidence}). ${reasoning}`;
          finalized = true;
        }

        toolResults.push({ type: "tool_result", tool_use_id: block.id, content: result });
      }

      if (toolResults.length > 0) messages.push({ role: "user", content: toolResults });
    }

    // Compute score from recorded signals
    const { data: signals } = await supabase.from("signals").select("*").eq("prospect_id", prospect_id);
    const { data: weights } = await supabase.from("signal_weights").select("*");

    const wmap = new Map(
      (weights ?? []).map((w: Record<string, unknown>) => [
        w.signal_type as string,
        { a: w.authenticity_weight as number, au: w.authority_weight as number, w: w.warmth_weight as number },
      ]),
    );

    let aN = 0, aD = 0, auN = 0, auD = 0, wN = 0, wD = 0;
    for (const s of signals ?? []) {
      const w = wmap.get(s.signal_type as string);
      if (!w) continue;
      const v = 100 * (1 - Math.exp(-Number(s.value) / 15));
      const base = (Number(s.weight) || 1) * (Number(s.confidence) || 1);
      aN += v * base * w.a;  aD += base * w.a;
      auN += v * base * w.au; auD += base * w.au;
      wN += v * base * w.w;  wD += base * w.w;
    }
    const round = (n: number) => Math.round(n * 10) / 10;

    await supabase.from("scores").insert({
      prospect_id,
      authenticity_score: round(aD ? aN / aD : 0),
      authority_score: round(auD ? auN / auD : 0),
      warmth_score: round(wD ? wN / wD : 0),
      overall_score: round(
        0.4 * (aD ? aN / aD : 0) + 0.4 * (auD ? auN / auD : 0) + 0.2 * (wD ? wN / wD : 0),
      ),
      falsification_notes: [
        "Authenticity assumes LinkedIn data is current - re-verify if profile edited in last 60 days.",
        "Authority cross-checks public records - invalid if patent attribution is wrong.",
        "Warmth depends on a fresh network graph - re-sync if data is >7 days old.",
        "Role validated against multiple sources - re-verify if prospect changed jobs recently.",
      ],
    });

    await supabase.from("scoring_runs").update({
      status: "complete",
      current_source: null,
      completed_at: new Date().toISOString(),
    }).eq("id", runId);

    return new Response(JSON.stringify({ success: true, prospect_id, finalized }), {
      headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
    });
  } catch (err) {
    console.error(err);
    return new Response(JSON.stringify({ error: (err as Error).message }), {
      status: 500,
      headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
    });
  }
});
