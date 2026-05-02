/**
 * DemoScript — floating "Demo Script" button (bottom-right, fixed) that
 * opens a panel with the per-case talking points used during the YC demo.
 * Per CONTRACTS.md Contract 5 §"UI requirements" and CLAUDE.md
 * §"The Demo Mode" (L843, L1000).
 *
 * Self-gated: returns null when not in demo mode.
 *
 * Talking-points data come from `src/lib/demoData.ts` (Track demoData,
 * LavenderPrairie). `DEMO_TALKING_POINTS` is `Record<string, string[]>`
 * keyed by the 5 stable demo UUIDs (`00000000-...-001..005`); each value
 * is a short array of bullets. Case titles + connection-type narratives
 * live below as `CASE_TITLES` because demoData.ts deliberately exports
 * data only, no presentation strings.
 */

import { useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { DEMO_PROSPECT_IDS, isDemoMode } from "@/store/graphStore"
import { DEMO_TALKING_POINTS } from "@/lib/demoData"

/**
 * Case title per demo UUID slot. Narrative mapping comes from demoData.ts
 * `[DONE demoData]` message and CONTRACTS.md Contract 5 — must stay in sync
 * with the prospects array there. If demoData.ts later renames a case,
 * update both.
 */
const CASE_TITLES: Record<string, string> = {
  [DEMO_PROSPECT_IDS[0]]: "Case 1 — Lin Wei × career-overlap",
  [DEMO_PROSPECT_IDS[1]]: "Case 2 — Ana Souza × conference co-presenter",
  [DEMO_PROSPECT_IDS[2]]: "Case 3 — Marcus Hale × patent co-invention",
  [DEMO_PROSPECT_IDS[3]]: "Case 4 — Priya Raman × academic co-authorship",
  [DEMO_PROSPECT_IDS[4]]: "Case 5 — Jonas Berg × standards committee peer",
}

export function DemoScript() {
  const [open, setOpen] = useState(false)
  if (!isDemoMode()) return null

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          size="sm"
          variant="default"
          className="fixed bottom-4 right-4 z-50 shadow-lg"
          aria-label="Open demo script"
        >
          Demo Script
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Demo Script — Credence v3</DialogTitle>
          <DialogDescription>
            Talking points per warm-path case. Source:{" "}
            <code className="font-mono text-[11px]">DEMO_CASES.md</code> via{" "}
            <code className="font-mono text-[11px]">demoData.ts</code>.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] space-y-3 overflow-y-auto pr-2">
          {DEMO_PROSPECT_IDS.map((id) => {
            const points = DEMO_TALKING_POINTS[id] ?? []
            const title = CASE_TITLES[id] ?? `Case ${id}`
            return (
              <section
                key={id}
                className="rounded-md border border-border bg-background/50 p-3"
              >
                <header className="mb-1 flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold">{title}</h3>
                </header>
                <ul className="space-y-1 text-sm text-muted-foreground">
                  {points.map((p, i) => (
                    <li key={i} className="flex gap-2">
                      <span aria-hidden="true">•</span>
                      <span>{p}</span>
                    </li>
                  ))}
                  {points.length === 0 ? (
                    <li className="italic text-muted-foreground/70">
                      No talking points yet.
                    </li>
                  ) : null}
                </ul>
              </section>
            )
          })}
        </div>
      </DialogContent>
    </Dialog>
  )
}
