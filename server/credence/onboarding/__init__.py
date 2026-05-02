"""Customer onboarding pipeline (CUSTOMER_ONBOARDING_PLAN.md).

Modules in this package implement the multi-stage scrape that runs when a
sales rep signs up:

  rep_resolver     — Stage 0: resolve the rep's LinkedIn from (name, email)
  team_scraper     — Stage 2: scrape the rep's company team (LinkedIn)
  entity_resolver  — Stage 2: 3-tier match (linkedin_url → name+co → INSERT)
  pipeline         — Stage orchestrator (Wave B, not in this commit)

The shared `account_team_members` + `onboarding_jobs` tables are defined in
`supabase/migrations/20260502_customer_onboarding.sql`. RLS-protected by
account_users membership.
"""
