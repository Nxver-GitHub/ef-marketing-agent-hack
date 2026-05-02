-- ╔════════════════════════════════════════════════════════════════════════╗
-- ║ prospect_warm_paths — denormalized top-K read cache for /discover     ║
-- ╚════════════════════════════════════════════════════════════════════════╝
--
-- Per-prospect top-K (default K=20) edge view. The frontend `/discover` page
-- previously paginated all `person_connections` (~46k rows + growing) and
-- joined `persons` twice in JS to translate person_id → source_prospect_id.
-- That collapses on Apify scale (~150k+ projected).
--
-- This table denormalizes the top-K edges per prospect so the read path is
-- O(40 visible prospects × K) instead of O(table). person_connections
-- remains the canonical write target for clustering jobs; this is a
-- read-cache refreshed by `server/credence/jobs/materialize_prospect_warm_paths.py`
-- after each clustering / enrichment run.
--
-- Tenancy: account_id NOT NULL with the anon-default-tenant SELECT bridge
-- so /discover unauth reads see the demo tenant's rows.

BEGIN;

CREATE TABLE IF NOT EXISTS public.prospect_warm_paths (
    -- (prospect_id, rank) is the natural PK — top-1 through top-K per prospect.
    prospect_id          uuid NOT NULL REFERENCES public.prospects(id) ON DELETE CASCADE,
    rank                 smallint NOT NULL,
    partner_prospect_id  uuid NOT NULL REFERENCES public.prospects(id) ON DELETE CASCADE,
    connection_type      text NOT NULL,
    computed_strength    numeric NOT NULL,
    -- Denormalized evidence: the structured_value of the highest-strength
    -- evidence row associated with this edge in connection_evidence (or
    -- null if the evidence couldn't be resolved). Capped to 4KB by the
    -- jsonb-size guard inherited from connection_evidence semantics.
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Denormalized partner display fields so the frontend can render the
    -- top-20 panel without a second prospects fetch.
    partner_name         text,
    partner_company      text,
    partner_title        text,
    account_id           uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    refreshed_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (prospect_id, rank),

    -- Same-prospect self-edges are nonsensical; person_connections
    -- enforces person_a < person_b but rank-by-prospect doesn't.
    CONSTRAINT prospect_warm_paths_no_self_edge
        CHECK (prospect_id <> partner_prospect_id),

    -- Rank in [1, 20] — keep the long tail out per user msg.
    CONSTRAINT prospect_warm_paths_rank_range
        CHECK (rank BETWEEN 1 AND 20),

    -- computed_strength is in [0, 0.99] (matches person_connections cap).
    CONSTRAINT prospect_warm_paths_strength_range
        CHECK (computed_strength >= 0 AND computed_strength <= 0.99),

    -- A prospect cannot appear as a partner more than once for the same
    -- prospect_id. (rank uniqueness is via PK; this catches duplicate-partner
    -- writes early.)
    CONSTRAINT prospect_warm_paths_unique_partner
        UNIQUE (prospect_id, partner_prospect_id)
);

-- Read-path indexes. The PK already covers (prospect_id, rank). Add a
-- (account_id, prospect_id) lookup for tenant-scoped fetches and a strength
-- index for "top edges across the tenant" queries.
CREATE INDEX IF NOT EXISTS idx_prospect_warm_paths_account_prospect
    ON public.prospect_warm_paths (account_id, prospect_id);

CREATE INDEX IF NOT EXISTS idx_prospect_warm_paths_strength_desc
    ON public.prospect_warm_paths (account_id, computed_strength DESC);

-- Connection-type filter (frontend pill toggles) — partial-friendly btree.
CREATE INDEX IF NOT EXISTS idx_prospect_warm_paths_connection_type
    ON public.prospect_warm_paths (connection_type);

-- ── RLS ────────────────────────────────────────────────────────────────────
-- Same shape as person_connections: tenant_isolation policy for authenticated
-- users + anon-default-tenant bridge so /discover unauth reads land.

ALTER TABLE public.prospect_warm_paths ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS prospect_warm_paths_tenant_isolation ON public.prospect_warm_paths;
CREATE POLICY prospect_warm_paths_tenant_isolation ON public.prospect_warm_paths
    FOR ALL TO authenticated
    USING (
        account_id IN (
            SELECT account_id FROM public.account_users
            WHERE user_id = auth.uid()
        )
    );

DROP POLICY IF EXISTS prospect_warm_paths_anon_default_select ON public.prospect_warm_paths;
CREATE POLICY prospect_warm_paths_anon_default_select ON public.prospect_warm_paths
    FOR SELECT TO anon
    USING (account_id = '00000000-0000-0000-0000-000000000001'::uuid);

COMMENT ON TABLE public.prospect_warm_paths IS
    'Denormalized top-K read cache for /discover. Refreshed by '
    'materialize_prospect_warm_paths.py after each clustering/enrichment run. '
    'person_connections remains the canonical write target.';

COMMIT;
