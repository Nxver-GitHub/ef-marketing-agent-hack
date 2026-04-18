/**
 * Service layer: external data fetchers.
 *
 * Each function returns a normalized signal payload:
 *   { source, signal_type, value, raw_data, weight, confidence }[]
 *
 * Stubs return realistic mock data tagged { _mock: true }. Swap the body
 * with a real fetch when wiring the source — interface stays identical.
 */

export type NormalizedSignal = {
  source: string;
  signal_type: string;
  value: number | string | Record<string, unknown>;
  raw_data: Record<string, unknown>;
  weight: number;
  confidence: number;
};

const rand = (min: number, max: number) => Math.floor(Math.random() * (max - min + 1)) + min;
const jitter = (base: number, spread = 0.15) =>
  +(base + (Math.random() - 0.5) * 2 * spread).toFixed(2);

// TODO(apify): https://console.apify.com/actors/M2FMdjRVeF1HPGFcc/input
export async function fetchLinkedInProfile(input: {
  url?: string;
  name: string;
  company: string;
}): Promise<NormalizedSignal[]> {
  const tenure = rand(1, 12);
  const recommendations = rand(0, 25);
  return [
    {
      source: "linkedin_profile",
      signal_type: "tenure_years",
      value: tenure,
      raw_data: { _mock: true, ...input, tenure_years: tenure },
      weight: 1.0,
      confidence: jitter(0.85),
    },
    {
      source: "linkedin_profile",
      signal_type: "recommendations",
      value: recommendations,
      raw_data: { _mock: true, recommendations },
      weight: 1.0,
      confidence: jitter(0.7),
    },
  ];
}

// TODO(apify): LinkedIn posts actor
export async function fetchLinkedInPosts(input: {
  name: string;
  company: string;
}): Promise<NormalizedSignal[]> {
  const posts_30d = rand(0, 30);
  return [
    {
      source: "linkedin_posts",
      signal_type: "post_activity",
      value: posts_30d,
      raw_data: { _mock: true, posts_30d, ...input },
      weight: 1.0,
      confidence: jitter(0.75),
    },
  ];
}

// TODO(uspto): https://developer.uspto.gov/api-catalog
export async function fetchUSPTOPatents(input: {
  name: string;
  company: string;
}): Promise<NormalizedSignal[]> {
  const patents = rand(0, 18);
  const citations = patents * rand(2, 12);
  return [
    {
      source: "uspto",
      signal_type: "patent_count",
      value: patents,
      raw_data: { _mock: true, patents, ...input },
      weight: 1.0,
      confidence: jitter(0.9),
    },
    {
      source: "uspto",
      signal_type: "patent_citations",
      value: citations,
      raw_data: { _mock: true, citations },
      weight: 1.0,
      confidence: jitter(0.85),
    },
  ];
}

// TODO(github): GitHub REST/GraphQL
export async function fetchGitHubActivity(input: { name: string }): Promise<NormalizedSignal[]> {
  const commits = rand(0, 800);
  return [
    {
      source: "github",
      signal_type: "github_commits",
      value: commits,
      raw_data: { _mock: true, commits, ...input },
      weight: 1.0,
      confidence: jitter(0.6),
    },
  ];
}

// TODO(scrape): conference/program scrapers
export async function fetchConferenceMentions(input: {
  name: string;
}): Promise<NormalizedSignal[]> {
  const talks = rand(0, 8);
  return [
    {
      source: "conference",
      signal_type: "conference_talks",
      value: talks,
      raw_data: { _mock: true, talks, ...input },
      weight: 1.0,
      confidence: jitter(0.7),
    },
  ];
}

// TODO(apify): Greenhouse/Ashby public scrape
export async function fetchCompanyHiring(input: {
  company: string;
  role: string;
}): Promise<NormalizedSignal[]> {
  const open_roles = rand(0, 25);
  return [
    {
      source: "company_hiring",
      signal_type: "hiring_signal",
      value: open_roles,
      raw_data: { _mock: true, open_roles, ...input },
      weight: 1.0,
      confidence: jitter(0.65),
    },
  ];
}

// TODO(crunchbase): Crunchbase API
export async function fetchCrunchbase(input: {
  name: string;
  company: string;
}): Promise<NormalizedSignal[]> {
  const verified = Math.random() > 0.3 ? 1 : 0;
  return [
    {
      source: "crunchbase",
      signal_type: "crunchbase_role",
      value: verified,
      raw_data: { _mock: true, verified, ...input },
      weight: 1.0,
      confidence: jitter(0.8),
    },
  ];
}

export async function fetchMutualConnections(input: {
  target_linkedin_url?: string;
  user_linkedin_url?: string;
  name: string;
}): Promise<NormalizedSignal[]> {
  const mutuals = rand(0, 35);
  return [
    {
      source: "mutual_connections",
      signal_type: "mutual_connections",
      value: mutuals,
      raw_data: { _mock: true, mutuals, ...input },
      weight: 1.0,
      confidence: jitter(0.75),
    },
  ];
}
