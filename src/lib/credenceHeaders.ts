/**
 * credenceHeaders — single source of truth for HTTP headers attached to
 * Credence API requests.
 *
 * Per CONTRACTS.md Contract 9 §"Demo mode reconciliation":
 *   When `?demo=true` is active, every backend fetch must include
 *   `X-Credence-Demo: true`. The FastAPI `auth.py:_demo_session` recognizes
 *   this header and binds the request to the demo pseudo-tenant (account_id
 *   00000000-0000-0000-0000-000000000fff) without JWT verification.
 *
 * Wave 6 M5 — demo-mode header wired (SwiftElk).
 * Wave 6 M3 — live-mode `Authorization: Bearer <supabase_jwt>` wiring
 * (LavenderPrairie). The token is set by AccountContext's auth-state-change
 * handler so this helper stays a pure synchronous function callable from
 * non-React contexts (`agent.ts`, `db.ts`).
 *
 * Callers should spread the result into their `headers` object:
 *
 *   await fetch(`${API_URL}/chat`, {
 *     method: "POST",
 *     headers: { "content-type": "application/json", ...getCredenceHeaders() },
 *     body: JSON.stringify(...),
 *   })
 */

import { isDemoMode } from "@/store/graphStore"

export type CredenceHeaders = Record<string, string>

// Module-local cache of the current Supabase access token. AccountContext
// keeps this in sync via `onAuthStateChange`; everywhere else just reads
// via `getCredenceHeaders()`. Never persisted — Supabase's own client
// handles refresh + storage; we just mirror the freshest token here.
let _activeAccessToken: string | null = null

/**
 * Update the cached access token. Called from AccountContext when the
 * Supabase auth state changes (sign in / sign out / token refresh).
 *
 * Pass `null` to clear (sign-out).
 */
export function setActiveAccessToken(token: string | null): void {
  _activeAccessToken = token
}

/**
 * Read the cached access token without exposing the full session shape.
 * Useful for debug logs and tests; production callers should use
 * `getCredenceHeaders()` instead.
 */
export function getActiveAccessToken(): string | null {
  return _activeAccessToken
}

/**
 * Resolve the per-request headers for a Credence backend call.
 *
 * - Signed in → `{ "Authorization": "Bearer <jwt>" }`
 * - Anyone else (demo URL, signed out, or auth disabled) → `{ "X-Credence-Demo": "true" }`
 *
 * Anonymous access is intentionally bound to the demo tenant since the
 * RequireAuth gate is disabled (open-access demo).
 *
 * Pure function. Safe to call from non-React contexts (`agent.ts`, `db.ts`).
 */
export function getCredenceHeaders(): CredenceHeaders {
  if (_activeAccessToken && !isDemoMode()) {
    return { Authorization: `Bearer ${_activeAccessToken}` }
  }
  return { "X-Credence-Demo": "true" }
}
