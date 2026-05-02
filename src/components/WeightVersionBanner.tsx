/**
 * Contract 6 §"UI banner contract" — surfaces stale-score state.
 *
 * Renders only when:
 *   1. We have a displayed score with a `weight_version_id` (i.e., it
 *      came from `score_records`, not v2 `scores` or the snapshot)
 *   2. We resolved the active version for the current tenant
 *   3. The two ids differ
 *
 * In every other case the banner is hidden — no flicker on demo mode,
 * snapshot mode, or while the active-version query is in flight.
 *
 * Backfill ETA is intentionally a placeholder: per Contract 6 §"UI banner
 * contract", `N` is meant to be `(prospects_remaining_in_backfill /
 * backfill_rate_per_min)` clamped `[1, 30]`. We don't have that telemetry
 * in the frontend yet, so we render a static "in the background" message.
 * Wire the live ETA when a `/score/backfill-status` endpoint lands.
 */

import { useActiveWeightVersion } from "@/lib/useActiveWeightVersion";

interface WeightVersionBannerProps {
  /** The `weight_version_id` of the score currently shown to the user. */
  readonly displayedVersionId?: string | null;
}

export function WeightVersionBanner({
  displayedVersionId,
}: WeightVersionBannerProps): JSX.Element | null {
  const active = useActiveWeightVersion();

  if (!displayedVersionId || !active || displayedVersionId === active.id) {
    return null;
  }

  return (
    <div
      role="status"
      className="border border-amber-500/40 bg-amber-500/5 px-3 py-2 mb-3 text-[11px] text-amber-200/90 leading-relaxed"
    >
      <span className="text-[10px] uppercase tracking-[0.16em] text-amber-300/80 mr-2">
        Stale score
      </span>
      Score computed with previous weights. Refresh is running in the background.
    </div>
  );
}
