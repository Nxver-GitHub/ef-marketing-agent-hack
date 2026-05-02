/**
 * DemoBanner — small fixed corner indicator that surfaces when the app is
 * running in demo mode (`?demo=true`). Per CONTRACTS.md Contract 5
 * §"UI requirements": "A subtle 'DEMO MODE' banner in the corner
 * (top-right, position: fixed), visible on every page when active."
 *
 * Self-gated: returns null when not in demo mode, so the parent can mount
 * it unconditionally at the app root.
 */

import { isDemoMode } from "@/store/graphStore"

export function DemoBanner() {
  if (!isDemoMode()) return null

  return (
    <div
      role="status"
      aria-label="Demo mode active"
      className="
        fixed top-3 right-3 z-50 select-none
        rounded-md border border-amber-500/40 bg-amber-500/10
        px-3 py-1
        text-[10px] font-semibold uppercase tracking-[0.2em]
        text-amber-700 dark:text-amber-300
        shadow-sm backdrop-blur-sm
      "
    >
      Demo mode
    </div>
  )
}
