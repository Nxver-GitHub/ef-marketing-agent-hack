/**
 * PersonAvatar — circular avatar with initials and a deterministic
 * color-from-name. Extracted from PersonProfileCard so OrgChart, Companies,
 * NodeInspector, and other surfaces can reuse the same widget.
 *
 * Pure presentational. Initials logic mirrors PersonProfileCard's
 * `computeInitials`: prefer first+last, fall back to first 2 chars of
 * canonical_name. Background color is a stable hash of canonical_name → one
 * of 8 muted Tailwind classes (so the same person always renders the same
 * color across pages).
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"

export interface PersonAvatarProps {
  person: {
    canonical_name: string
    first_name?: string | null
    last_name?: string | null
  }
  size?: "xs" | "sm" | "md" | "lg" | "xl"
  className?: string
  showBorder?: boolean
}

// ── Pure helpers (exported for tests) ───────────────────────────────────────

/** First+last initial; falls back to first 2 chars of canonical_name. */
export function computeInitials(person: PersonAvatarProps["person"]): string {
  const f = (person.first_name ?? "").trim()
  const l = (person.last_name ?? "").trim()
  if (f.length > 0 || l.length > 0) {
    const a = f.charAt(0)
    const b = l.charAt(0)
    const out = `${a}${b}`.trim()
    if (out.length > 0) return out.toUpperCase()
  }
  const canon = (person.canonical_name ?? "").trim()
  return canon.slice(0, 2).toUpperCase()
}

/** djb2-ish hash; deterministic across runs and platforms. */
export function avatarHash(name: string): number {
  let h = 5381
  for (let i = 0; i < name.length; i += 1) {
    h = ((h << 5) + h + name.charCodeAt(i)) & 0xffffffff
  }
  return Math.abs(h)
}

/** 8 muted background classes — enough variation, low visual noise. */
export const AVATAR_PALETTE: ReadonlyArray<string> = [
  "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100",
  "bg-stone-200 text-stone-800 dark:bg-stone-700 dark:text-stone-100",
  "bg-rose-200 text-rose-900 dark:bg-rose-900/60 dark:text-rose-100",
  "bg-amber-200 text-amber-900 dark:bg-amber-900/60 dark:text-amber-100",
  "bg-emerald-200 text-emerald-900 dark:bg-emerald-900/60 dark:text-emerald-100",
  "bg-sky-200 text-sky-900 dark:bg-sky-900/60 dark:text-sky-100",
  "bg-violet-200 text-violet-900 dark:bg-violet-900/60 dark:text-violet-100",
  "bg-fuchsia-200 text-fuchsia-900 dark:bg-fuchsia-900/60 dark:text-fuchsia-100",
]

export function avatarColorClass(name: string): string {
  return AVATAR_PALETTE[avatarHash(name) % AVATAR_PALETTE.length]
}

const SIZE_CLASSES: Record<
  NonNullable<PersonAvatarProps["size"]>,
  string
> = {
  xs: "w-6 h-6 text-[9px]",
  sm: "w-8 h-8 text-[10px]",
  md: "w-10 h-10 text-xs",
  lg: "w-12 h-12 text-sm",
  xl: "w-16 h-16 text-base",
}

// ── Component ──────────────────────────────────────────────────────────────

export function PersonAvatar({
  person,
  size = "md",
  className,
  showBorder = true,
}: PersonAvatarProps): JSX.Element {
  const initials = computeInitials(person)
  const colorClass = avatarColorClass(person.canonical_name ?? "")
  const sz = SIZE_CLASSES[size]
  return (
    <span
      data-testid="person-avatar"
      data-size={size}
      data-initials={initials}
      aria-label={person.canonical_name || initials}
      role="img"
      className={cn(
        "inline-flex items-center justify-center rounded-full font-semibold shrink-0",
        sz,
        colorClass,
        showBorder && "ring-1 ring-slate-300 dark:ring-slate-600",
        className,
      )}
    >
      {initials}
    </span>
  )
}

export default PersonAvatar
