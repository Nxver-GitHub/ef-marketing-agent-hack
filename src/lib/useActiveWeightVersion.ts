/**
 * Wave 6 / Contract 6 §"UI banner contract" — exposes the currently-active
 * `score_weights.id` for the signed-in tenant.
 *
 * Used by `NodeInspector.tsx`'s WeightVersionBanner: when a displayed
 * `score_records.weight_version_id` differs from this hook's value, the
 * banner explains "Score computed with previous weights — refresh in N min".
 *
 * Demo mode + snapshot mode short-circuit to `null` (banner stays hidden).
 * If a Supabase query fails we also return null and log to console — the
 * banner is supplementary UI, not a blocker.
 *
 * ## Cache invalidation (Stream D phase 2)
 *
 * `Settings.tsx` calls `invalidateActiveWeightVersion()` after a successful
 * sub-score-mix save so every consumer of this hook re-fetches and the
 * banner can appear on prospects whose displayed score is now stale.
 * Implementation is a module-level counter + listener set, kept inside this
 * module so the hook stays a black box to consumers — no React context
 * needed for what is just one tenant-scoped cache key.
 */

import { useEffect, useState } from "react";

import { supabase, HAS_REAL_SUPABASE } from "@/lib/supabase";
import { useAccount } from "@/contexts/AccountContext";
import { isDemoAccount } from "@/lib/account";

export interface ActiveWeightVersion {
  readonly id: string;
  readonly authenticityW: number;
  readonly authorityW: number;
  readonly warmthW: number;
}

// Module-local invalidation. Bumping this counter wakes every mounted
// useActiveWeightVersion hook so they re-query Supabase. Lives outside
// React state because there is exactly one logical cache here (the
// signed-in tenant's active version) — a context would be ceremony for
// no benefit.
let _refreshCounter = 0;
const _listeners = new Set<() => void>();

/**
 * Force every mounted useActiveWeightVersion hook to re-query.
 *
 * Call after any write that could change the active row — i.e., the
 * Settings.tsx save that flips is_active. Synchronous; the actual fetch
 * fires inside each subscriber's useEffect on the next tick.
 */
export function invalidateActiveWeightVersion(): void {
  _refreshCounter += 1;
  _listeners.forEach((fn) => fn());
}

/** Returns the active `score_weights` row for the current tenant, or null. */
export function useActiveWeightVersion(): ActiveWeightVersion | null {
  const accountState = useAccount();
  const [version, setVersion] = useState<ActiveWeightVersion | null>(null);
  const [refreshKey, setRefreshKey] = useState(_refreshCounter);

  // Subscribe to invalidation broadcasts.
  useEffect(() => {
    const listener = () => setRefreshKey(_refreshCounter);
    _listeners.add(listener);
    return () => {
      _listeners.delete(listener);
    };
  }, []);

  useEffect(() => {
    if (
      isDemoAccount(accountState) ||
      !HAS_REAL_SUPABASE ||
      !supabase ||
      !accountState.account
    ) {
      setVersion(null);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const { data, error } = await supabase
          .from("score_weights")
          .select("id, authenticity_w, authority_w, warmth_w")
          .eq("account_id", accountState.account!.id)
          .eq("is_active", true)
          .maybeSingle();
        if (cancelled) return;
        if (error) {
          console.warn("[useActiveWeightVersion] supabase error:", error.message);
          setVersion(null);
          return;
        }
        if (!data) {
          setVersion(null);
          return;
        }
        setVersion({
          id: data.id as string,
          authenticityW: Number(data.authenticity_w),
          authorityW: Number(data.authority_w),
          warmthW: Number(data.warmth_w),
        });
      } catch (err) {
        if (cancelled) return;
        console.warn("[useActiveWeightVersion] threw:", err);
        setVersion(null);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [accountState, refreshKey]);

  return version;
}
