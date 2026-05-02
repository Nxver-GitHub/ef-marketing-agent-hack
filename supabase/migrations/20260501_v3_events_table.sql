-- 2026-05-01: events table — precursor for B1 conference_attendances FK
-- (LavenderPrairie, applied during the v3.1 migration round-up).
--
-- ## Why this exists
--
-- B1 (`20260501_v3_education_conference.sql`) was authored against a
-- canonical `public.events` table per CLAUDE.md L70:
--
-- > events — conferences, workshops, standards meetings, award ceremonies.
-- > event_appearances — who appeared at each event, in what role
-- > (presenter, panelist, session_chair, attendee, keynote).
--
-- The connection-graph migration (`20260430_v3_connection_graph.sql`)
-- created persons / companies / employment_periods / patents /
-- patent_inventors / person_connections / connection_evidence but
-- skipped events + event_appearances. B1's `conference_attendances.event_id
-- REFERENCES public.events(id)` therefore failed apply.
--
-- Creating events here as a thin precursor — schema follows the CLAUDE.md
-- intent: an event has name + year + kind (conference/workshop/standards/
-- award) and a venue is captured as a free-text string for v1. RLS +
-- account_id wiring matches the rest of the v3 multitenant stack.
--
-- Not creating event_appearances yet — B1 ships its own
-- conference_attendances which serves the same role for the conference
-- extractor work; the more general event_appearances can land in a future
-- migration once a use case actually consumes it.

BEGIN;

CREATE TABLE IF NOT EXISTS public.events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL DEFAULT 'conference',
  year            INTEGER NOT NULL,
  venue           TEXT,
  url             TEXT,
  source          TEXT NOT NULL DEFAULT 'manual',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT events_kind_keyspace
    CHECK (kind IN ('conference', 'workshop', 'standards_meeting', 'award_ceremony')),
  CONSTRAINT events_year_range
    CHECK (year >= 1900 AND year <= 2100),
  CONSTRAINT events_source_keyspace
    CHECK (source IN ('firecrawl', 'parallel', 'scholar', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_events_account_id ON public.events (account_id);
CREATE INDEX IF NOT EXISTS idx_events_name_year ON public.events (name, year);
CREATE INDEX IF NOT EXISTS idx_events_year ON public.events (year);

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS events_tenant_isolation ON public.events;
CREATE POLICY events_tenant_isolation ON public.events
  FOR ALL TO authenticated
  USING (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()))
  WITH CHECK (account_id IN (SELECT au.account_id FROM public.account_users au WHERE au.user_id = auth.uid()));

DROP POLICY IF EXISTS events_anon_default_select ON public.events;
CREATE POLICY events_anon_default_select ON public.events
  FOR SELECT TO anon
  USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);

COMMIT;
