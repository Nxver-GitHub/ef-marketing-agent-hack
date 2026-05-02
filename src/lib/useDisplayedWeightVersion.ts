/**
 * Reads the `weight_version_id` of the most-recent `score_records` row for a
 * given prospect. Used by `WeightVersionBanner` to detect "displayed score
 * was computed under an older weight set than the active one."
 *
 * Why this is a separate hook instead of folding into the existing
 * `useScoreFor` / `toScore`: the v2 `scores` table has no `weight_version_id`
 * column, and `score_records` has a different shape (`falsification_note`
 * singular vs `falsification_notes` array). Merging the rows would require a
 * schema reconciliation that's out of scope for the banner ship. Reading
 * just the `weight_version_id` independently keeps the existing score-fetch
 * path untouched while still wiring the banner.
 *
 * Demo + snapshot mode short-circuit to null. Errors and empty result both
 * yield null (banner stays hidden). Re-fetches when
 * `invalidateActiveWeightVersion()` fires so a Settings save also wakes
 * displayed-side caches.
 */

import { useEffect, useState } from "react";

import { supabase, HAS_REAL_SUPABASE } from "@/lib/supabase";
import { useAccount } from "@/contexts/AccountContext";
import { isDemoAccount } from "@/lib/account";

const USE_SNAPSHOT =
  HAS_REAL_SUPABASE &&
  (import.meta.env.VITE_USE_SNAPSHOT as string | undefined) === "true";

export function useDisplayedWeightVersion(
  prospectId: string | undefined,
): string | null {
  const accountState = useAccount();
  const [versionId, setVersionId] = useState<string | null>(null);

  useEffect(() => {
    if (
      !prospectId ||
      isDemoAccount(accountState) ||
      USE_SNAPSHOT ||
      !HAS_REAL_SUPABASE ||
      !supabase
    ) {
      setVersionId(null);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const { data, error } = await supabase
          .from("score_records")
          .select("weight_version_id")
          .eq("prospect_id", prospectId)
          .order("computed_at", { ascending: false })
          .limit(1)
          .maybeSingle();
        if (cancelled) return;
        if (error) {
          console.warn("[useDisplayedWeightVersion] supabase error:", error.message);
          setVersionId(null);
          return;
        }
        setVersionId(data?.weight_version_id ?? null);
      } catch (err) {
        if (cancelled) return;
        console.warn("[useDisplayedWeightVersion] threw:", err);
        setVersionId(null);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [prospectId, accountState]);

  return versionId;
}
