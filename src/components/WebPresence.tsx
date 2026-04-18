/**
 * Renders the "Web presence" section on ProspectDetail — bio, education,
 * career history, publications, conference talks, news mentions, and social
 * links — all sourced from `public.signals` rows tagged `source:"web_enrichment"`
 * (or any signal whose `signal_type` matches one of the enrichment kinds).
 *
 * Hidden when a prospect has no enrichment signals (most bulk-imported rows).
 */
import type { Signal } from "@/lib/db";

type Degree = { school?: string; degree?: string; field?: string };
type CareerRole = { company?: string; role?: string; years?: string };
type Publication = { title?: string; type?: string; year?: string | null };
type Talk = { event?: string; title?: string; year?: string; url?: string };
type NewsItem = { title?: string; date?: string; url?: string | null };
type SocialLink = { platform?: string; url?: string; label?: string };

type Props = { signals: Signal[] };

const ENRICHMENT_SOURCE = "web_enrichment";

function byType<T>(signals: Signal[], t: string): T[] {
  return signals
    .filter((s) => s.signal_type === t && s.value && typeof s.value === "object")
    .map((s) => s.value as T);
}

export function WebPresence({ signals }: Props) {
  const bios = byType<{ text?: string }>(signals, "bio");
  const educations = byType<{ degrees?: Degree[] }>(signals, "education");
  const careers = byType<{ roles?: CareerRole[] }>(signals, "career_history");
  const publications = byType<Publication>(signals, "publication");
  const talks = byType<Talk>(signals, "conference_talk");
  const news = byType<NewsItem>(signals, "news_mention");
  const socials = byType<SocialLink>(signals, "social_link");

  const hasAny =
    bios.length ||
    educations.length ||
    careers.length ||
    publications.length ||
    talks.length ||
    news.length ||
    socials.length;
  if (!hasAny) return null;

  const enrichmentSources = Array.from(
    new Set(
      signals
        .filter((s) => s.source === ENRICHMENT_SOURCE || isEnrichmentKind(s.signal_type))
        .map((s) => s.source),
    ),
  );

  return (
    <div className="border border-border">
      <div className="p-5 border-b border-border flex items-center justify-between">
        <div>
          <div className="label-eyebrow mb-1">Web presence</div>
          <div className="text-xs text-muted-foreground">
            {countSections(bios, educations, careers, publications, talks, news, socials)} sections ·{" "}
            {enrichmentSources.join(", ") || "public sources"}
          </div>
        </div>
      </div>

      <div className="divide-y divide-border/60">
        {bios.length > 0 && (
          <Section label="Bio">
            {bios.map((b, i) => (
              <p key={i} className="text-sm leading-relaxed">
                {b.text}
              </p>
            ))}
          </Section>
        )}

        {careers.length > 0 && (
          <Section label="Career history">
            <ul className="space-y-2">
              {careers
                .flatMap((c) => c.roles ?? [])
                .map((r, i) => (
                  <li key={i} className="text-sm flex justify-between gap-4">
                    <span>
                      <span className="font-medium">{r.company}</span>
                      <span className="text-muted-foreground"> — {r.role}</span>
                    </span>
                    {r.years && (
                      <span className="text-mono text-xs text-muted-foreground/70 shrink-0">
                        {r.years}
                      </span>
                    )}
                  </li>
                ))}
            </ul>
          </Section>
        )}

        {educations.length > 0 && (
          <Section label="Education">
            <ul className="space-y-1.5">
              {educations
                .flatMap((e) => e.degrees ?? [])
                .map((d, i) => (
                  <li key={i} className="text-sm">
                    <span className="font-medium">{d.school}</span>
                    {d.degree && <span className="text-muted-foreground"> — {d.degree}</span>}
                    {d.field && <span className="text-muted-foreground/70">, {d.field}</span>}
                  </li>
                ))}
            </ul>
          </Section>
        )}

        {talks.length > 0 && (
          <Section label={`Conference talks (${talks.length})`}>
            <ul className="space-y-2">
              {talks.map((t, i) => (
                <li key={i} className="text-sm">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="font-medium">{t.event}</span>
                    {t.year && (
                      <span className="text-mono text-xs text-muted-foreground/70 shrink-0">
                        {t.year}
                      </span>
                    )}
                  </div>
                  {t.title && (
                    <div className="text-xs text-muted-foreground mt-0.5">{t.title}</div>
                  )}
                  {t.url && (
                    <a
                      href={t.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[11px] underline text-muted-foreground/70 hover:text-foreground"
                    >
                      recording ↗
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {publications.length > 0 && (
          <Section label="Publications">
            <ul className="space-y-1">
              {publications.map((p, i) => (
                <li key={i} className="text-sm">
                  <span className="font-medium">{p.title}</span>
                  {p.type && <span className="text-muted-foreground/70"> — {p.type}</span>}
                  {p.year && <span className="text-muted-foreground/70"> ({p.year})</span>}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {news.length > 0 && (
          <Section label="Recent news mentions">
            <ul className="space-y-1.5">
              {news.map((n, i) => (
                <li key={i} className="text-sm">
                  {n.url ? (
                    <a
                      href={n.url}
                      target="_blank"
                      rel="noreferrer"
                      className="underline underline-offset-2 hover:text-foreground"
                    >
                      {n.title}
                    </a>
                  ) : (
                    <span>{n.title}</span>
                  )}
                  {n.date && (
                    <span className="text-mono text-xs text-muted-foreground/70 ml-2">
                      {n.date}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {socials.length > 0 && (
          <Section label="Social / web">
            <div className="flex flex-wrap gap-2">
              {socials.map((s, i) => (
                <a
                  key={i}
                  href={s.url ?? "#"}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[11px] uppercase tracking-[0.14em] px-2.5 py-1 border border-border hover:bg-secondary transition-colors"
                >
                  {s.label ?? s.platform ?? s.url} ↗
                </a>
              ))}
            </div>
          </Section>
        )}
      </div>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="p-5">
      <div className="label-eyebrow mb-3">{label}</div>
      {children}
    </div>
  );
}

function countSections(...groups: unknown[][]): number {
  return groups.filter((g) => g.length > 0).length;
}

function isEnrichmentKind(t: string): boolean {
  return [
    "bio",
    "education",
    "career_history",
    "publication",
    "conference_talk",
    "news_mention",
    "social_link",
  ].includes(t);
}
