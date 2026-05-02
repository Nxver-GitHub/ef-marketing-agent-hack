# Chat Tools Plan: Warm Paths + Org Context

> **Author:** LavenderPrairie  
> **Date:** 2026-05-02  
> **Status:** APPROVED — ready to implement  
> **Estimated effort:** ~14 hr total  
> **Depends on:** CONTRACTS.md Contracts 2, 7, 10 (existing); new Contract 12 + 13 below

---

## Why These Two Tools

The chat agent has four tools today: `focus_node`, `filter`, `explain`, `expand_node`. All four are lookup tools — they describe what exists. Neither can answer the two most important sales questions:

1. **"Who at my team knows this person?"** — requires BFS over `person_connections`
2. **"Who does this person report to, and what do they own?"** — requires `org_reporting_edges` + `person_scope_estimates`

Both tables are live and populated. Neither is reachable from the agent. These two tools close that gap.

---

## Tool 1 — `find_warm_paths`

### What it does

Given a target person (the prospect the rep is researching), find the ranked warm introduction paths from any "connector" person in the database. Returns up to 10 paths sorted by path strength descending, each with a human-readable explanation and a suggested outreach opener.

### Tool registration in `chat.py`

```python
{
    "name": "find_warm_paths",
    "description": (
        "Find warm introduction paths between the user's team and a target prospect. "
        "Use when asked 'who knows this person?', 'how can I get introduced to X?', "
        "or 'find a warm path to [name]'. Returns ranked paths with explanation and "
        "suggested outreach opener for each."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_id": {
                "type": "string",
                "description": "UUID of the target person node."
            },
            "max_hops": {
                "type": "integer",
                "description": "Maximum path length (default 3, max 4).",
                "default": 3
            },
            "min_strength": {
                "type": "number",
                "description": "Minimum path strength threshold (default 0.30).",
                "default": 0.30
            },
            "connection_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Filter to specific connection types. "
                    "Valid values: patent_co_inventor, academic_co_author, "
                    "career_overlap_same_team, career_overlap_same_domain, "
                    "conference_co_presenter, standards_committee_peer, "
                    "same_phd_advisor, co_board_member. "
                    "Omit to include all warm types."
                )
            }
        },
        "required": ["target_id"]
    }
}
```

### Implementation: `server/credence/search.py`

```python
WARM_CONNECTION_TYPES = {
    "patent_co_inventor",
    "academic_co_author_multi",
    "academic_co_author_single",
    "career_overlap_same_team",
    "career_overlap_same_domain",
    "career_overlap_general",
    "conference_co_presenter",
    "standards_committee_peer",
    "same_phd_advisor",
    "co_board_member",
    "co_investor",
}

async def find_warm_paths(
    target_person_id: str,
    max_hops: int = 3,
    min_strength: float = 0.30,
    connection_types: list[str] | None = None,
) -> dict:
    """
    BFS over person_connections from target outward to find the strongest
    introduction paths. Returns top 10 paths sorted by path_strength desc.

    Algorithm:
      - person_connections is pre-materialized and indexed on computed_strength
      - person_a_id < person_b_id always enforced — query both sides every hop
      - Path strength = product of edge computed_strength values
      - Prune branches where path_strength < min_strength
      - Dedup: if the same connector appears via two paths, keep the stronger one
    """
    supabase = get_supabase()
    allowed_types = set(connection_types) if connection_types else WARM_CONNECTION_TYPES

    # --- Hop 0: load direct connections to the target ---
    hop0 = _fetch_connections_for(supabase, target_person_id, allowed_types, min_strength)

    # Each entry: { neighbor_id, edge_id, connection_type, computed_strength,
    #               evidence_summary, path_nodes: [target_id, neighbor_id],
    #               path_edges: [edge_id], path_strength }
    frontier: list[dict] = []
    for conn in hop0:
        neighbor = conn["person_a_id"] if conn["person_b_id"] == target_person_id \
                   else conn["person_b_id"]
        frontier.append({
            "connector_id":  neighbor,
            "path_nodes":    [target_person_id, neighbor],
            "path_edges":    [conn["id"]],
            "path_strength": conn["computed_strength"],
            "raw_edges":     [conn],
        })

    completed_paths = []   # paths whose connector_id is a "known" person (see below)
    visited = {target_person_id}

    for hop in range(1, max_hops):
        next_frontier = []
        for branch in frontier:
            tip = branch["connector_id"]
            if tip in visited:
                continue
            visited.add(tip)

            neighbors = _fetch_connections_for(supabase, tip, allowed_types, min_strength)
            for conn in neighbors:
                nbr = conn["person_a_id"] if conn["person_b_id"] == tip else conn["person_b_id"]
                if nbr in branch["path_nodes"]:
                    continue  # no cycles

                new_strength = branch["path_strength"] * conn["computed_strength"]
                if new_strength < min_strength:
                    continue

                new_branch = {
                    "connector_id":  nbr,
                    "path_nodes":    branch["path_nodes"] + [nbr],
                    "path_edges":    branch["path_edges"] + [conn["id"]],
                    "path_strength": new_strength,
                    "raw_edges":     branch["raw_edges"] + [conn],
                }
                next_frontier.append(new_branch)

        frontier = next_frontier

    # All branches (including hop-0 direct connections) are candidate paths.
    # "Completed" means the connector end is a real person in the DB.
    # Sort by strength, dedup by connector_id (keep strongest per connector).
    all_paths = [b for b in frontier] + \
                [b for b in [
                    {
                        "connector_id":  f["connector_id"],
                        "path_nodes":    f["path_nodes"],
                        "path_edges":    f["path_edges"],
                        "path_strength": f["path_strength"],
                        "raw_edges":     f["raw_edges"],
                    }
                    for f in frontier
                ]]

    # Re-fetch from hop0 direct results too
    all_paths = []
    for branch in frontier:
        all_paths.append(branch)
    # Re-add hop0 direct connections as 1-hop paths
    for conn in hop0:
        neighbor = conn["person_a_id"] if conn["person_b_id"] == target_person_id \
                   else conn["person_b_id"]
        all_paths.append({
            "connector_id":  neighbor,
            "path_nodes":    [target_person_id, neighbor],
            "path_edges":    [conn["id"]],
            "path_strength": conn["computed_strength"],
            "raw_edges":     [conn],
        })

    # Dedup by connector_id, keep strongest
    best_by_connector: dict[str, dict] = {}
    for path in all_paths:
        cid = path["connector_id"]
        if cid not in best_by_connector or \
           path["path_strength"] > best_by_connector[cid]["path_strength"]:
            best_by_connector[cid] = path

    top_paths = sorted(best_by_connector.values(),
                       key=lambda p: p["path_strength"], reverse=True)[:10]

    if not top_paths:
        return {
            "target_id": target_person_id,
            "paths_found": 0,
            "paths": [],
            "message": "No warm paths found in the current graph. Try expanding the graph or lowering min_strength."
        }

    # Hydrate person names
    all_person_ids = {pid for p in top_paths for pid in p["path_nodes"]}
    persons = _fetch_persons_by_ids(supabase, all_person_ids)
    person_map = {p["id"]: p for p in persons}

    rendered = []
    for path in top_paths:
        nodes = [person_map.get(pid, {"id": pid, "canonical_name": "Unknown"})
                 for pid in path["path_nodes"]]
        rendered.append({
            "path_strength":    round(path["path_strength"], 3),
            "hops":             len(path["path_nodes"]) - 1,
            "connector":        nodes[-1].get("canonical_name"),
            "connector_id":     path["connector_id"],
            "path_names":       [n.get("canonical_name", "?") for n in nodes],
            "connection_types": [e["connection_type"] for e in path["raw_edges"]],
            "explanation":      _build_explanation(nodes, path["raw_edges"]),
            "suggested_opener": _build_opener(nodes, path["raw_edges"]),
        })

    return {
        "target_id":   target_person_id,
        "target_name": person_map.get(target_person_id, {}).get("canonical_name"),
        "paths_found": len(rendered),
        "paths":       rendered,
    }


def _fetch_connections_for(
    supabase,
    person_id: str,
    allowed_types: set[str],
    min_strength: float,
) -> list[dict]:
    """Fetch all person_connections rows touching person_id."""
    result = supabase.table("person_connections") \
        .select("id, person_a_id, person_b_id, connection_type, computed_strength, evidence_ids") \
        .or_(f"person_a_id.eq.{person_id},person_b_id.eq.{person_id}") \
        .gte("computed_strength", min_strength) \
        .in_("connection_type", list(allowed_types)) \
        .order("computed_strength", desc=True) \
        .limit(50) \
        .execute()
    return result.data or []


def _fetch_persons_by_ids(supabase, ids: set[str]) -> list[dict]:
    if not ids:
        return []
    result = supabase.table("persons") \
        .select("id, canonical_name, current_title, current_company_id") \
        .in_("id", list(ids)) \
        .execute()
    return result.data or []


def _build_explanation(nodes: list[dict], edges: list[dict]) -> str:
    """Generate a specific, non-generic explanation for the first edge."""
    if not edges:
        return "Direct connection."
    first_edge = edges[0]
    a_name = nodes[0].get("canonical_name", "Person A")
    b_name = nodes[1].get("canonical_name", "Person B") if len(nodes) > 1 else "Person B"
    ev = first_edge.get("evidence_summary") or {}
    ctype = first_edge.get("connection_type", "")

    if ctype == "patent_co_inventor":
        return (f"{a_name} and {b_name} co-invented "
                f"{ev.get('patent_title', 'a patent')} "
                f"({ev.get('assignee', 'shared employer')}, {ev.get('year', 'year unknown')})")
    if ctype in ("academic_co_author_multi", "academic_co_author_single"):
        return (f"{a_name} and {b_name} co-authored "
                f"\"{ev.get('paper_title', 'a paper')}\" "
                f"at {ev.get('venue', 'a conference')} ({ev.get('year', 'year unknown')}, "
                f"{ev.get('citation_count', 0)} citations)")
    if ctype == "standards_committee_peer":
        return (f"{a_name} and {b_name} served on the "
                f"{ev.get('committee', 'standards committee')} together "
                f"({ev.get('years', 'active period unknown')})")
    if ctype == "conference_co_presenter":
        return (f"{a_name} and {b_name} co-presented at "
                f"{ev.get('event', 'a conference')} ({ev.get('year', 'year unknown')})")
    if ctype in ("career_overlap_same_team", "career_overlap_same_domain", "career_overlap_general"):
        return (f"{a_name} and {b_name} worked together at "
                f"{ev.get('company', 'a shared employer')} "
                f"({ev.get('overlap_start', '?')}–{ev.get('overlap_end', '?')}, "
                f"{ev.get('overlap_years', '?')} yr overlap)")
    if ctype == "same_phd_advisor":
        return (f"{a_name} and {b_name} share a PhD advisor: "
                f"{ev.get('advisor_name', 'same advisor')} at "
                f"{ev.get('institution', 'their shared institution')}")
    return (f"{a_name} and {b_name} have a "
            f"{ctype.replace('_', ' ')} connection")


def _build_opener(nodes: list[dict], edges: list[dict]) -> str:
    """Generate the first sentence of the outreach email."""
    if not edges or len(nodes) < 2:
        return ""
    connector = nodes[-1].get("canonical_name", "Your contact")
    first_edge = edges[0]
    ev = first_edge.get("evidence_summary") or {}
    ctype = first_edge.get("connection_type", "")

    if ctype == "patent_co_inventor":
        return (f"{connector} — we co-invented {ev.get('patent_title', 'a patent')} "
                f"together at {ev.get('assignee', 'our shared employer')} "
                f"back in {ev.get('year', 'years past')}.")
    if ctype in ("academic_co_author_multi", "academic_co_author_single"):
        return (f"{connector} — we co-authored "
                f"\"{ev.get('paper_title', 'a paper')}\" "
                f"at {ev.get('venue', 'a conference')}.")
    if ctype == "standards_committee_peer":
        return (f"{connector} — we sat on the "
                f"{ev.get('committee', 'same standards committee')} together.")
    if ctype in ("career_overlap_same_team", "career_overlap_same_domain", "career_overlap_general"):
        return (f"{connector} — we worked together at "
                f"{ev.get('company', 'the same company')}.")
    if ctype == "same_phd_advisor":
        return (f"{connector} — we both worked under "
                f"{ev.get('advisor_name', 'the same advisor')} at "
                f"{ev.get('institution', 'grad school')}.")
    return f"{connector} — we've crossed paths before and I wanted to reconnect."
```

### Dispatch in `chat.py`

```python
elif tool_use.name == "find_warm_paths":
    result = await find_warm_paths(
        target_person_id = args["target_id"],
        max_hops         = args.get("max_hops", 3),
        min_strength     = args.get("min_strength", 0.30),
        connection_types = args.get("connection_types"),
    )
```

### Definition of Done

- [ ] `find_warm_paths(target_id)` returns a populated response for any person with edges in `person_connections`
- [ ] Returns empty `paths` array (not error) when no paths exist
- [ ] Path strength is product of edge strengths, not average or sum
- [ ] Explanation strings are specific: patent title, company name, year — not "have a connection"
- [ ] Suggested opener is a complete sentence
- [ ] Agent correctly calls this tool when user asks "who knows this person?" or "find a warm intro to X"
- [ ] Max 10 paths returned, sorted strongest first
- [ ] Handles `max_hops=1` (direct connections only) correctly

**Effort:** 6 hr

---

## Tool 2 — `get_org_context`

### What it does

Given a person, returns their position in the inferred org chart: who they report to, who reports to them, their functional cluster peers, and what they own (from `person_scope_estimates`). Answers "where does this person sit in the organization?" and "what is their budget/decision authority?"

### Tool registration in `chat.py`

```python
{
    "name": "get_org_context",
    "description": (
        "Get the org chart context for a person: their manager, direct reports, "
        "functional peers, and scope/budget estimates. Use when asked 'who does X report to?', "
        "'who are X's direct reports?', 'what does X own?', 'what is X's budget authority?', "
        "or 'where does X sit in the org?'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "person_id": {
                "type": "string",
                "description": "UUID of the person to get org context for."
            },
            "include_peers": {
                "type": "boolean",
                "description": "Whether to include functional cluster peers (default true).",
                "default": True
            }
        },
        "required": ["person_id"]
    }
}
```

### Implementation: `server/credence/search.py`

```python
async def get_org_context(
    person_id: str,
    include_peers: bool = True,
) -> dict:
    """
    Return org chart context for a person from org_reporting_edges,
    org_functional_clusters, org_cluster_members, and person_scope_estimates.

    Queries:
      1. org_reporting_edges WHERE child_person_id = person_id AND is_current = true
         → their manager(s), with confidence + inference_method
      2. org_reporting_edges WHERE parent_person_id = person_id AND is_current = true
         → their direct reports, with confidence
      3. org_cluster_members JOIN org_functional_clusters
         WHERE person_id = person_id
         → their functional cluster, domain label, cluster peers
      4. person_scope_estimates WHERE person_id = person_id
         → owns_products, owns_functions, team_size, budget_authority_level
    """
    supabase = get_supabase()

    # --- Query 1: manager(s) ---
    manager_rows = supabase.table("org_reporting_edges") \
        .select("""
            id, confidence, path_confidence, inference_method,
            valid_from, valid_to, is_current,
            parent:parent_person_id (
                id, canonical_name, current_title, current_seniority_score,
                current_functional_domain
            )
        """) \
        .eq("child_person_id", person_id) \
        .eq("is_current", True) \
        .order("confidence", desc=True) \
        .execute()

    managers = []
    for row in (manager_rows.data or []):
        p = row.get("parent") or {}
        managers.append({
            "person_id":         p.get("id"),
            "name":              p.get("canonical_name"),
            "title":             p.get("current_title"),
            "seniority_score":   p.get("current_seniority_score"),
            "functional_domain": p.get("current_functional_domain"),
            "edge_confidence":   row.get("confidence"),
            "inference_method":  row.get("inference_method"),
            "is_dotted_line":    row.get("path_confidence", 1.0) < row.get("confidence", 1.0),
        })

    # --- Query 2: direct reports ---
    report_rows = supabase.table("org_reporting_edges") \
        .select("""
            id, confidence, inference_method,
            child:child_person_id (
                id, canonical_name, current_title, current_seniority_score,
                current_functional_domain
            )
        """) \
        .eq("parent_person_id", person_id) \
        .eq("is_current", True) \
        .order("confidence", desc=True) \
        .execute()

    direct_reports = []
    for row in (report_rows.data or []):
        c = row.get("child") or {}
        direct_reports.append({
            "person_id":         c.get("id"),
            "name":              c.get("canonical_name"),
            "title":             c.get("current_title"),
            "seniority_score":   c.get("current_seniority_score"),
            "functional_domain": c.get("current_functional_domain"),
            "edge_confidence":   row.get("confidence"),
            "inference_method":  row.get("inference_method"),
        })

    # --- Query 3: functional cluster + peers ---
    cluster_peers = []
    cluster_info = None
    if include_peers:
        membership_row = supabase.table("org_cluster_members") \
            .select("cluster_id, membership_confidence") \
            .eq("person_id", person_id) \
            .order("membership_confidence", desc=True) \
            .limit(1) \
            .execute()

        if membership_row.data:
            cluster_id = membership_row.data[0]["cluster_id"]

            cluster_row = supabase.table("org_functional_clusters") \
                .select("functional_domain, sub_domain, member_count, company_id") \
                .eq("id", cluster_id) \
                .single() \
                .execute()
            cluster_info = cluster_row.data

            peer_rows = supabase.table("org_cluster_members") \
                .select("""
                    membership_confidence,
                    person:person_id (
                        id, canonical_name, current_title, current_seniority_score
                    )
                """) \
                .eq("cluster_id", cluster_id) \
                .neq("person_id", person_id) \
                .order("membership_confidence", desc=True) \
                .limit(10) \
                .execute()

            for row in (peer_rows.data or []):
                p = row.get("person") or {}
                cluster_peers.append({
                    "person_id":           p.get("id"),
                    "name":                p.get("canonical_name"),
                    "title":               p.get("current_title"),
                    "seniority_score":     p.get("current_seniority_score"),
                    "membership_confidence": row.get("membership_confidence"),
                })

    # --- Query 4: scope estimates ---
    scope_row = supabase.table("person_scope_estimates") \
        .select("*") \
        .eq("person_id", person_id) \
        .limit(1) \
        .execute()

    scope = scope_row.data[0] if scope_row.data else {}

    # --- Person identity ---
    person_row = supabase.table("persons") \
        .select("id, canonical_name, current_title, current_seniority_score, current_functional_domain, current_company_id") \
        .eq("id", person_id) \
        .single() \
        .execute()
    person = person_row.data or {}

    return {
        "person": {
            "id":                person.get("id"),
            "name":              person.get("canonical_name"),
            "title":             person.get("current_title"),
            "seniority_score":   person.get("current_seniority_score"),
            "functional_domain": person.get("current_functional_domain"),
        },
        "managers":        managers,
        "direct_reports":  direct_reports,
        "direct_report_count": len(direct_reports),
        "functional_cluster": {
            "domain":     cluster_info.get("functional_domain") if cluster_info else None,
            "sub_domain": cluster_info.get("sub_domain") if cluster_info else None,
            "peers":      cluster_peers,
            "peer_count": cluster_info.get("member_count", len(cluster_peers)) if cluster_info else 0,
        },
        "scope": {
            "owns_products":        scope.get("owns_products", []),
            "owns_technologies":    scope.get("owns_technologies", []),
            "owns_functions":       scope.get("owns_functions", []),
            "owns_regions":         scope.get("owns_regions", []),
            "team_size_min":        scope.get("team_size_min"),
            "team_size_max":        scope.get("team_size_max"),
            "budget_authority_level": scope.get("budget_authority_level"),
        },
        "org_chart_note": (
            "Org chart edges are inferred probabilistically. "
            "High-confidence edges (≥0.7) use explicit signals (job postings, press releases). "
            "Low-confidence edges use seniority + domain clustering."
        ) if managers or direct_reports else None,
    }
```

### Dispatch in `chat.py`

```python
elif tool_use.name == "get_org_context":
    result = await get_org_context(
        person_id     = args["person_id"],
        include_peers = args.get("include_peers", True),
    )
```

### Definition of Done

- [ ] `get_org_context(person_id)` returns manager(s) and direct reports for any person with edges in `org_reporting_edges`
- [ ] Returns empty arrays (not error) when no org edges exist for this person
- [ ] `inference_method` is included on each edge so the agent can qualify uncertain edges
- [ ] `budget_authority_level` and `owns_products` surfaces from `person_scope_estimates` when present
- [ ] Agent correctly calls this tool when asked "who does X report to?", "who are X's reports?", or "what does X own?"
- [ ] `cluster_peers` returns up to 10 functional peers with names and titles
- [ ] Dotted-line relationships are flagged (`is_dotted_line: true`) when `path_confidence < confidence`

**Effort:** 4 hr

---

## System Prompt Addition

Both tools will be more useful if the agent knows what's available. Add to the static system prompt in `chat.py`:

```
You have two new tools available beyond the original four:

find_warm_paths — Use this whenever the user asks how to get introduced to a person,
who knows a person, or how warm a connection is. Always call this before suggesting
cold outreach. If paths are found, lead your response with the strongest path's
explanation and suggested opener. If no paths are found, say so explicitly.

get_org_context — Use this whenever the user asks about reporting relationships,
org chart position, scope of responsibility, or budget ownership. When edge
confidence is below 0.5, qualify the response: "This is inferred from job posting
language and may not reflect current reality."

Combine tools when needed: if a user asks "who at my team knows the person who
manages NVIDIA's HBM program?", first use get_org_context to find who manages
HBM, then use find_warm_paths to find connections to that person.
```

---

## File Map

```
server/credence/
└── search.py     MODIFY — add find_warm_paths(), get_org_context(),
                           _fetch_connections_for(), _fetch_persons_by_ids(),
                           _build_explanation(), _build_opener()

server/credence/
└── chat.py       MODIFY — register 2 new tools in tools list,
                           add elif dispatch blocks,
                           update system prompt
```

No schema changes required. No new dependencies. Both functions use existing Supabase tables with existing indexes.

---

## Contract 12 — `find_warm_paths`

```
Function:  search.find_warm_paths(target_person_id, max_hops, min_strength, connection_types)
Returns:   { target_id, target_name, paths_found, paths: WarmPathResult[] }

WarmPathResult:
  path_strength:    float   -- product of all edge computed_strength values
  hops:             int     -- number of edges in path
  connector:        str     -- name of person at the source end of path
  connector_id:     str     -- UUID of connector
  path_names:       str[]   -- names of all nodes from target → connector
  connection_types: str[]   -- connection_type for each edge in path
  explanation:      str     -- specific human-readable explanation of first edge
  suggested_opener: str     -- first sentence of outreach email

Invariants:
  - path_strength = product of edge computed_strength values, not sum or average
  - all paths pruned where path_strength < min_strength
  - at most 10 paths returned, sorted by path_strength desc
  - empty paths array returned (not error) when no paths found
  - no path contains a cycle (no repeated person IDs)
```

## Contract 13 — `get_org_context`

```
Function:  search.get_org_context(person_id, include_peers)
Returns:   { person, managers[], direct_reports[], direct_report_count,
             functional_cluster, scope, org_chart_note }

Invariants:
  - managers and direct_reports are empty arrays (not null) when no edges exist
  - each manager/report includes edge_confidence and inference_method
  - is_dotted_line = true when path_confidence < confidence on the edge
  - scope fields are empty arrays / null (not missing) when person_scope_estimates has no row
  - up to 10 functional peers returned in functional_cluster.peers
```

---

## Total Estimate: ~14 hr

| Task | Effort |
|---|---|
| `find_warm_paths` in search.py + helpers | 6 hr |
| `get_org_context` in search.py | 4 hr |
| Tool registration + dispatch in chat.py | 1.5 hr |
| System prompt update | 0.5 hr |
| Manual testing against live DB | 2 hr |
| **Total** | **14 hr** |
