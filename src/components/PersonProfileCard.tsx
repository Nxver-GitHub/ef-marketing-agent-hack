/**
 * PersonProfileCard — rich, presentational identity card for a `persons` row.
 *
 * Pure presentational: caller passes in the person object (no fetching, no
 * useState, no useEffect). Designed to surface every piece of LinkedIn-grade
 * enrichment we have on a single person — identity, badges, reach, contact —
 * in three discrete rows that gracefully collapse when fields are null.
 *
 * Styling follows the project's existing right-rail card patterns from
 * NodeInspector.tsx (border + bg-card + p-* containers, muted-foreground for
 * labels, monospaced text-mono for numbers, uppercase tracking-wider eyebrows).
 */
import type { JSX } from "react";
import { cn } from "@/lib/utils";

// ── Types ───────────────────────────────────────────────────────────────────

export type EmailStatus = "verified" | "guessed" | "unverified" | "unavailable";

export interface PersonProfileCardProps {
  person: {
    canonical_name: string;
    first_name?: string | null;
    last_name?: string | null;
    linkedin_url?: string | null;
    email?: string | null;
    email_status?: EmailStatus | null;
    current_title?: string | null;
    current_company_name?: string | null;
    location_text?: string | null;
    country_code?: string | null;
    headline?: string | null;
    connections_count?: number | null;
    followers_count?: number | null;
    premium?: boolean | null;
    verified?: boolean | null;
    open_to_work?: boolean | null;
    hiring?: boolean | null;
    registered_at?: string | null;
  };
  className?: string;
}

// ── Pure helpers (exported for testability) ─────────────────────────────────

/**
 * Convert an ISO 3166-1 alpha-2 country code to its regional-indicator flag
 * emoji (e.g. "US" → "🇺🇸"). Returns "" for any input that isn't exactly two
 * ASCII letters — Unicode regional indicators are only defined for A-Z.
 */
export function flagEmoji(cc: string | null | undefined): string {
  if (!cc || typeof cc !== "string") return "";
  const trimmed = cc.trim();
  if (trimmed.length !== 2) return "";
  const upper = trimmed.toUpperCase();
  // Reject any non-ASCII-letter character — Unicode flags require A-Z.
  if (!/^[A-Z]{2}$/.test(upper)) return "";
  const A = 0x41; // 'A'
  const REGIONAL_INDICATOR_A = 0x1f1e6;
  const codePoints = [
    REGIONAL_INDICATOR_A + (upper.charCodeAt(0) - A),
    REGIONAL_INDICATOR_A + (upper.charCodeAt(1) - A),
  ];
  return String.fromCodePoint(...codePoints);
}

/** Format a number with US-style comma grouping (1582 → "1,582"). */
export function formatCount(n: number | null | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "";
  return new Intl.NumberFormat("en-US").format(n);
}

/** Format an ISO datetime as "Mon YYYY" (e.g. "Feb 2014"). Returns "" on failure. */
export function formatRegisteredAt(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    year: "numeric",
  }).format(d);
}

/** Compute initials for the avatar — prefers first+last, falls back to canonical_name. */
export function computeInitials(person: PersonProfileCardProps["person"]): string {
  const f = (person.first_name ?? "").trim();
  const l = (person.last_name ?? "").trim();
  if (f.length > 0 || l.length > 0) {
    const a = f.charAt(0);
    const b = l.charAt(0);
    const out = `${a}${b}`.trim();
    if (out.length > 0) return out.toUpperCase();
  }
  const canon = (person.canonical_name ?? "").trim();
  return canon.slice(0, 2).toUpperCase();
}

/** Extract the slug from a LinkedIn URL (e.g. "/in/jenhsunhuang" → "jenhsunhuang"). */
export function linkedinSlug(url: string): string {
  const match = url.match(/\/in\/([^/?#]+)/i);
  if (match && match[1]) return match[1];
  // Fallback: last non-empty path segment.
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    return parts[parts.length - 1] ?? url;
  } catch {
    return url;
  }
}

// ── Sub-components (local; not exported) ────────────────────────────────────

const Eyebrow = ({ children }: { children: React.ReactNode }) => (
  <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
    {children}
  </div>
);

interface BadgeSpec {
  show: boolean;
  label: string;
  testId: string;
  className: string;
}

function BadgeRow({ badges }: { badges: BadgeSpec[] }): JSX.Element | null {
  const visible = badges.filter((b) => b.show);
  if (visible.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {visible.map((b) => (
        <span
          key={b.testId}
          data-testid={b.testId}
          className={cn(
            "inline-flex items-center text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 border rounded-sm",
            b.className,
          )}
        >
          {b.label}
        </span>
      ))}
    </div>
  );
}

function StatCell({
  value,
  label,
  testId,
}: {
  value: string;
  label: string;
  testId: string;
}): JSX.Element {
  return (
    <div data-testid={testId} className="bg-card p-3 space-y-1 min-w-0">
      <div className="text-mono text-base font-medium truncate">{value}</div>
      <Eyebrow>{label}</Eyebrow>
    </div>
  );
}

const EMAIL_STATUS_PILL: Record<
  Exclude<EmailStatus, "unavailable">,
  { label: string; className: string }
> = {
  verified: {
    label: "Verified email",
    className: "bg-emerald-500/10 text-emerald-600 border-emerald-500/30",
  },
  guessed: {
    label: "Guessed",
    className: "bg-yellow-500/10 text-yellow-700 border-yellow-500/30",
  },
  unverified: {
    label: "Unverified",
    className: "bg-muted text-muted-foreground border-border",
  },
};

// ── Main ────────────────────────────────────────────────────────────────────

export function PersonProfileCard(props: PersonProfileCardProps): JSX.Element {
  const { person, className } = props;

  const initials = computeInitials(person);
  const flag = flagEmoji(person.country_code ?? null);
  const hasLocationRow = Boolean(flag) || Boolean((person.location_text ?? "").trim());

  // Badges — only render when boolean is strictly true.
  const badges: BadgeSpec[] = [
    {
      show: person.premium === true,
      label: "Premium",
      testId: "badge-premium",
      className: "bg-amber-400/15 text-amber-700 border-amber-500/40",
    },
    {
      show: person.verified === true,
      label: "Verified",
      testId: "badge-verified",
      className: "bg-blue-500/10 text-blue-600 border-blue-500/30",
    },
    {
      show: person.open_to_work === true,
      label: "Open to Work",
      testId: "badge-open-to-work",
      className: "bg-emerald-500/10 text-emerald-600 border-emerald-500/30",
    },
    {
      show: person.hiring === true,
      label: "Hiring",
      testId: "badge-hiring",
      className: "bg-purple-500/10 text-purple-600 border-purple-500/30",
    },
  ];

  // Row 2 (reach) — collect non-null cells.
  const connectionsValue =
    typeof person.connections_count === "number"
      ? formatCount(person.connections_count)
      : "";
  const followersValue =
    typeof person.followers_count === "number"
      ? formatCount(person.followers_count)
      : "";
  const registeredValue = formatRegisteredAt(person.registered_at ?? null);

  const reachCells: { value: string; label: string; testId: string }[] = [];
  if (connectionsValue) {
    reachCells.push({
      value: connectionsValue,
      label: "connections",
      testId: "reach-connections",
    });
  }
  if (followersValue) {
    reachCells.push({
      value: followersValue,
      label: "followers",
      testId: "reach-followers",
    });
  }
  if (registeredValue) {
    reachCells.push({
      value: registeredValue,
      label: "on LinkedIn",
      testId: "reach-registered",
    });
  }

  // Row 3 (contact) — email + linkedin.
  const showEmailRow =
    Boolean((person.email ?? "").trim()) &&
    person.email_status !== "unavailable";
  const showLinkedinRow = Boolean((person.linkedin_url ?? "").trim());
  const hasContactRow = showEmailRow || showLinkedinRow;

  const emailPill =
    person.email_status &&
    person.email_status !== "unavailable" &&
    EMAIL_STATUS_PILL[person.email_status];

  const titleLine = [person.current_title, person.current_company_name]
    .filter((s): s is string => Boolean(s && s.trim()))
    .join(" · ");

  return (
    <article
      data-testid="person-profile-card"
      className={cn(
        "rounded-lg border border-border bg-card p-4 space-y-4",
        className,
      )}
    >
      {/* ── Row 1 — Identity ────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <div
            data-testid="profile-avatar"
            aria-hidden="true"
            className="w-12 h-12 shrink-0 rounded-full bg-muted border border-border flex items-center justify-center text-sm font-medium text-foreground/80 select-none"
          >
            {initials}
          </div>
          <div className="min-w-0 pt-0.5 space-y-1">
            <h2 className="text-base font-semibold leading-tight truncate">
              {person.canonical_name}
            </h2>
            {person.headline && (
              <div
                data-testid="profile-headline"
                className="text-xs text-muted-foreground line-clamp-2 leading-snug"
              >
                {person.headline}
              </div>
            )}
            {!person.headline && titleLine && (
              <div className="text-xs text-muted-foreground line-clamp-2 leading-snug">
                {titleLine}
              </div>
            )}
            {hasLocationRow && (
              <div
                data-testid="profile-location"
                className="text-[11px] text-muted-foreground flex items-center gap-1.5 mt-1"
              >
                {flag && (
                  <span aria-hidden="true" className="text-sm leading-none">
                    {flag}
                  </span>
                )}
                {person.location_text && (
                  <span className="truncate">{person.location_text}</span>
                )}
              </div>
            )}
          </div>
        </div>
        <BadgeRow badges={badges} />
      </div>

      {/* ── Row 2 — Reach ─────────────────────────────────────────────── */}
      {reachCells.length > 0 && (
        <>
          <div className="border-t border-border" />
          <div
            data-testid="profile-reach"
            className={cn(
              "grid gap-px bg-border border border-border",
              reachCells.length === 1 && "grid-cols-1",
              reachCells.length === 2 && "grid-cols-2",
              reachCells.length === 3 && "grid-cols-3",
            )}
          >
            {reachCells.map((c) => (
              <StatCell
                key={c.testId}
                value={c.value}
                label={c.label}
                testId={c.testId}
              />
            ))}
          </div>
        </>
      )}

      {/* ── Row 3 — Contact ───────────────────────────────────────────── */}
      {hasContactRow && (
        <>
          <div className="border-t border-border" />
          <div data-testid="profile-contact" className="space-y-2">
            {showEmailRow && (
              <div
                data-testid="profile-email"
                className="flex items-center gap-2 flex-wrap min-w-0"
              >
                <a
                  href={`mailto:${person.email}`}
                  className="text-xs text-foreground hover:underline truncate min-w-0"
                >
                  {person.email}
                </a>
                {emailPill && (
                  <span
                    data-testid={`email-pill-${person.email_status}`}
                    className={cn(
                      "inline-flex items-center text-[10px] uppercase tracking-[0.14em] px-1.5 py-0.5 border rounded-sm shrink-0",
                      emailPill.className,
                    )}
                  >
                    {emailPill.label}
                  </span>
                )}
              </div>
            )}
            {showLinkedinRow && person.linkedin_url && (
              <div data-testid="profile-linkedin" className="text-xs min-w-0">
                <a
                  data-testid="linkedin-link"
                  href={person.linkedin_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-foreground hover:underline truncate inline-block max-w-full"
                >
                  in/{linkedinSlug(person.linkedin_url)}
                </a>
              </div>
            )}
          </div>
        </>
      )}
    </article>
  );
}
