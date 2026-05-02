"""Org-chart pipeline (v3.1 Plan A).

Three stages:

1. **Population** — clustering, hierarchy, scope estimation
2. **Quality measurement** — corrections capture, performance tracking
3. **Optimization** — weight tuning, span validation, confidence propagation

Each stage is a separate module so subagents can be assigned
non-overlapping work. Hard ordering is documented in V3_PT2.md.

The package writes to six tables created by
``20260501_v3_orgchart_schema.sql``:

- ``org_functional_clusters`` (Stage 1.1 — clustering)
- ``org_cluster_members`` (Stage 1.1 — clustering)
- ``org_reporting_edges`` (Stage 1.2 — hierarchy)
- ``person_scope_estimates`` (Stage 1.3 — scope)
- ``org_chart_corrections`` (Stage 2.1 — corrections capture)
- ``org_signal_performance`` (Stage 2.2 — performance tracker)
"""
