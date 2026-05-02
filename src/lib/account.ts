/**
 * Wave 6 M3 — frontend tenant context (LavenderPrairie skeleton).
 *
 * Wraps the React tree in an `AccountProvider` so every consumer can
 * read the current `Account` without prop-drilling. The provider:
 *
 *   1. Resolves the current Supabase Auth session on mount
 *   2. Looks up the user's account via `account_users` join
 *   3. Exposes `{ account, user, loading, error }` via context
 *   4. Short-circuits to the demo pseudo-tenant when `?demo=true` is set
 *
 * Per CONTRACTS.md Contract 9 §"Demo mode reconciliation", demo mode
 * skips Supabase Auth entirely — the AccountProvider returns a synthetic
 * Account with id `00000000-0000-0000-0000-000000000fff`.
 *
 * ## Skeleton state
 *
 * The actual React Context + Provider component lives in
 * `src/contexts/AccountContext.tsx` (also part of M3 — wired alongside this
 * module). This file holds the shared types + pure helpers that don't
 * depend on React, so they're trivially unit-testable without rendering.
 */

import { isDemoMode } from "@/store/graphStore";

/** Stable UUIDs seeded by the multitenant migration. */
export const DEMO_ACCOUNT_ID = "00000000-0000-0000-0000-000000000fff";
export const DEFAULT_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001";

/** Plan tier surfaced from `accounts.plan_tier`. */
export type PlanTier = "free" | "pro" | "enterprise";

/** Subset of the v1 `accounts` row the frontend needs. */
export interface Account {
  readonly id: string;
  readonly displayName: string;
  readonly slug: string;
  readonly planTier: PlanTier;
}

/**
 * Subset of `auth.users` the frontend uses. We don't surface the email or
 * any other PII unnecessary for routing — just enough to greet the user
 * by name in the TopBar.
 */
export interface User {
  readonly id: string;
  readonly email: string;
  readonly displayName: string | null;
}

/** Aggregate session state exposed via `useAccount()`. */
export interface AccountState {
  readonly account: Account | null;
  readonly user: User | null;
  readonly loading: boolean;
  readonly error: string | null;
}

// ── Demo-mode synthetic account ─────────────────────────────────────────────

/**
 * In demo mode, we don't talk to Supabase Auth at all — return a synthetic
 * account that matches the migration-seeded demo pseudo-tenant.
 */
const DEMO_ACCOUNT: Account = {
  id: DEMO_ACCOUNT_ID,
  displayName: "Demo Account",
  slug: "demo",
  planTier: "free",
};

const DEMO_USER: User = {
  id: "00000000-0000-0000-0000-0000000000d1",
  email: "demo@credence.local",
  displayName: "Demo User",
};

/**
 * Initial state for the provider — captures the demo short-circuit before
 * any network resolution happens. Cuts the demo-mode boot path to a
 * single synchronous render with no flashes of "loading".
 */
export function initialAccountState(): AccountState {
  if (isDemoMode()) {
    return {
      account: DEMO_ACCOUNT,
      user: DEMO_USER,
      loading: false,
      error: null,
    };
  }
  return {
    account: null,
    user: null,
    loading: true,
    error: null,
  };
}

// ── Pure helpers (no React, no Supabase) ────────────────────────────────────

/** True if the current state represents the demo pseudo-tenant. */
export function isDemoAccount(state: AccountState): boolean {
  return state.account?.id === DEMO_ACCOUNT_ID;
}

/** True when the user is authenticated AND has been assigned to an account. */
export function isResolved(state: AccountState): boolean {
  return !state.loading && state.account !== null && state.user !== null;
}

/**
 * Map a raw Supabase `accounts` row (snake_case) to the camelCase
 * `Account` shape the frontend uses. Tolerates missing optional fields.
 */
export function fromSupabaseAccount(row: {
  id: string;
  display_name: string;
  slug: string;
  plan_tier?: string;
}): Account {
  const tier: PlanTier =
    row.plan_tier === "pro" || row.plan_tier === "enterprise"
      ? row.plan_tier
      : "free";
  return {
    id: row.id,
    displayName: row.display_name,
    slug: row.slug,
    planTier: tier,
  };
}

/**
 * Map a Supabase `auth.users` row to the frontend `User` shape. The
 * provider extracts `display_name` from `user_metadata.full_name` when
 * present (Supabase's standard convention for "given name + family name");
 * falls back to the email's local-part otherwise.
 */
export function fromSupabaseUser(row: {
  id: string;
  email?: string | null;
  user_metadata?: { full_name?: string | null } | null;
}): User {
  const email = row.email ?? "";
  const fromMeta = row.user_metadata?.full_name ?? null;
  const displayName = fromMeta || email.split("@")[0] || null;
  return {
    id: row.id,
    email,
    displayName,
  };
}
