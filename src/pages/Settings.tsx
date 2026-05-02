import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { PageShell } from "@/components/PageShell";
import { Slider } from "@/components/ui/slider";
import { useWeights, useProspects, db, type SignalWeight } from "@/lib/db";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import { HAS_REAL_SUPABASE, supabase } from "@/lib/supabase";
import { useAccount } from "@/contexts/AccountContext";
import { isDemoAccount } from "@/lib/account";
import {
  invalidateActiveWeightVersion,
  useActiveWeightVersion,
  type ActiveWeightVersion,
} from "@/lib/useActiveWeightVersion";

const USE_SNAPSHOT =
  HAS_REAL_SUPABASE && (import.meta.env.VITE_USE_SNAPSHOT as string | undefined) === "true";

const Settings = () => {
  useDocumentTitle("Settings");
  const weights = useWeights();
  const prospects = useProspects();
  const [draft, setDraft] = useState<Record<string, [number, number, number]>>({});
  const [saving, setSaving] = useState(false);
  const dirty = Object.keys(draft).length;

  const get = (signal_type: string, idx: 0 | 1 | 2, fallback: number) =>
    draft[signal_type]?.[idx] ?? fallback;

  const setVal = (signal_type: string, idx: 0 | 1 | 2, v: number, w: SignalWeight) => {
    const cur = draft[signal_type] ?? [
      w.authenticity_weight,
      w.authority_weight,
      w.warmth_weight,
    ];
    const next: [number, number, number] = [cur[0], cur[1], cur[2]];
    next[idx] = v;
    setDraft({ ...draft, [signal_type]: next });
  };

  const reset = () => setDraft({});

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    const changed = Object.keys(draft).length;
    try {
      for (const [signal_type, vals] of Object.entries(draft)) {
        await db.upsertWeight(signal_type, vals[0], vals[1], vals[2]);
      }
      await db.computeScores(prospects.map((p) => p._id));
      setDraft({});
      toast.success(
        `Saved ${changed} weight${changed === 1 ? "" : "s"} — recomputed ${prospects.length} prospect${prospects.length === 1 ? "" : "s"}.`,
      );
    } catch (err) {
      console.error("[Settings] save failed:", err);
      toast.error("Couldn't save weights. Check console for details.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <PageShell>
      <div className="space-y-16">
        <SubScoreMixSection />
        <div className="grid md:grid-cols-12 gap-10">
          <div className="md:col-span-4">
            <div className="label-eyebrow mb-3">Settings</div>
            <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05] mb-6">
              Signal weights.
            </h1>
            <p className="text-sm text-muted-foreground mb-6 leading-relaxed">
              Tune how each signal contributes to the three sub-scores. Saving
              recomputes every prospect immediately. Scoring code never hardcodes
              weights — they live here.
            </p>

            {USE_SNAPSHOT && (
              <div className="border border-amber-500/40 bg-amber-500/5 px-4 py-3 mb-6 text-xs text-amber-200/90 leading-relaxed">
                <div className="text-[10px] uppercase tracking-[0.16em] text-amber-300/80 mb-1">
                  Snapshot mode
                </div>
                Saves write to Supabase, but the offline JSON snapshot won't
                reflect new scores until you re-run{" "}
                <span className="text-mono">scripts/snapshot-supabase.mjs</span>.
              </div>
            )}

            <div className="flex items-center gap-3 flex-wrap">
              <button
                onClick={save}
                disabled={!dirty || saving}
                aria-busy={saving}
                className="border border-foreground bg-foreground text-background px-5 py-2.5 text-xs uppercase tracking-[0.16em] disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {saving
                  ? "Saving…"
                  : dirty
                    ? `Save & recompute · ${dirty}`
                    : "No changes"}
              </button>
              {dirty > 0 && !saving && (
                <button
                  onClick={reset}
                  className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground hover:text-foreground transition-colors"
                >
                  Reset {dirty} unsaved
                </button>
              )}
            </div>

            <div className="text-[10px] text-muted-foreground mt-8 leading-relaxed">
              <div className="label-eyebrow mb-2">Reading the math</div>
              Each signal contributes <span className="text-mono">norm × confidence × weight</span> to
              the corresponding sub-score. Sub-scores combine as{" "}
              <span className="text-mono">0.4 · auth + 0.4 · authority + 0.2 · warmth</span>{" "}
              by default — adjust the mix above.
            </div>
          </div>

          <div className="md:col-span-8">
            <div className="border border-border">
              <div className="grid grid-cols-12 px-4 py-3 text-[10px] uppercase tracking-[0.16em] text-muted-foreground border-b border-border">
                <div className="col-span-5">Signal type</div>
                <div className="col-span-2 text-right">Authenticity</div>
                <div className="col-span-2 text-right">Authority</div>
                <div className="col-span-3 text-right">Warmth</div>
              </div>
              {weights.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                  No signal weights loaded yet…
                </div>
              ) : (
                weights.map((w) => (
                  <div
                    key={w._id}
                    className="grid grid-cols-12 items-center px-4 py-3 border-b border-border/60 last:border-0"
                  >
                    <div className="col-span-5 text-sm text-mono">{w.signal_type}</div>
                    <WeightInput
                      v={get(w.signal_type, 0, w.authenticity_weight)}
                      onChange={(v) => setVal(w.signal_type, 0, v, w)}
                      dirty={draft[w.signal_type]?.[0] !== undefined}
                    />
                    <WeightInput
                      v={get(w.signal_type, 1, w.authority_weight)}
                      onChange={(v) => setVal(w.signal_type, 1, v, w)}
                      dirty={draft[w.signal_type]?.[1] !== undefined}
                    />
                    <WeightInput
                      v={get(w.signal_type, 2, w.warmth_weight)}
                      onChange={(v) => setVal(w.signal_type, 2, v, w)}
                      cols={3}
                      dirty={draft[w.signal_type]?.[2] !== undefined}
                    />
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </PageShell>
  );
};

const WeightInput = ({
  v,
  onChange,
  cols = 2,
  dirty = false,
}: {
  v: number;
  onChange: (v: number) => void;
  cols?: 2 | 3;
  dirty?: boolean;
}) => (
  <div className={`${cols === 3 ? "col-span-3" : "col-span-2"} flex justify-end`}>
    <input
      type="number"
      step={0.1}
      min={0}
      max={2}
      value={v}
      onChange={(e) => onChange(Number(e.target.value))}
      className={`w-20 text-right bg-transparent border px-2 py-1 text-mono text-xs focus:outline-none focus:border-accent transition-colors ${
        dirty ? "border-accent text-foreground" : "border-border"
      }`}
    />
  </div>
);

// ─── Sub-score mix (Wave 6 Contract 6 phase 2) ──────────────────────────────
// Edits the three top-level weights on `score_weights` (authenticity_w /
// authority_w / warmth_w). Sum-to-1 invariant enforced both client-side
// (auto-rebalance on slider drag, integer percentages) and server-side
// (sum_to_one CHECK in `20260430_v3_score_versioning.sql`).
//
// Save uses the SECURITY INVOKER `replace_active_score_weights` RPC from
// `20260430_v3_score_weights_replace_rpc.sql` — atomic flip-then-insert so
// the partial unique index on (account_id) WHERE is_active = TRUE is never
// violated and tenants always have exactly one active version.

type SubScoreDraft = { auth: number; authority: number; warmth: number };

// Default fallback when no active row exists yet (e.g., new tenant where the
// migration seed somehow didn't run for this account). Matches the canonical
// 0.4 / 0.4 / 0.2 baseline from CLAUDE.md "Scoring Model".
const FALLBACK_DRAFT: SubScoreDraft = { auth: 40, authority: 40, warmth: 20 };

const fromActive = (active: ActiveWeightVersion | null): SubScoreDraft =>
  active
    ? {
        auth: Math.round(active.authenticityW * 100),
        authority: Math.round(active.authorityW * 100),
        warmth: Math.round(active.warmthW * 100),
      }
    : FALLBACK_DRAFT;

const eq = (a: SubScoreDraft, b: SubScoreDraft): boolean =>
  a.auth === b.auth && a.authority === b.authority && a.warmth === b.warmth;

// Drag one slider; redistribute the delta proportionally across the other two
// so all three integer percentages still sum to 100. When the other two are
// both at 0 we split evenly. Last "other" gets the rounding remainder so the
// sum is exact, never 99 or 101.
const rebalance = (
  current: SubScoreDraft,
  which: keyof SubScoreDraft,
  rawNext: number,
): SubScoreDraft => {
  const clamped = Math.max(0, Math.min(100, Math.round(rawNext)));
  const otherTotal = 100 - clamped;
  const others = (["auth", "authority", "warmth"] as const).filter((k) => k !== which);
  const oldOtherSum = others.reduce((s, k) => s + current[k], 0);

  const next = { ...current, [which]: clamped };
  if (oldOtherSum > 0) {
    let allocated = 0;
    others.forEach((k, i) => {
      const share =
        i === others.length - 1
          ? otherTotal - allocated
          : Math.round(otherTotal * (current[k] / oldOtherSum));
      next[k] = share;
      allocated += share;
    });
  } else {
    next[others[0]] = Math.floor(otherTotal / 2);
    next[others[1]] = otherTotal - next[others[0]];
  }
  return next;
};

const SubScoreMixSection = () => {
  const accountState = useAccount();
  const active = useActiveWeightVersion();
  const isDemo = isDemoAccount(accountState);
  const canEdit = HAS_REAL_SUPABASE && !!supabase && !!accountState.account && !isDemo;

  const baseline = useMemo(() => fromActive(active), [active]);
  const [draft, setDraft] = useState<SubScoreDraft>(baseline);
  const [saving, setSaving] = useState(false);

  // Reset draft when the active version changes (e.g., another tab saved,
  // or the hook just finished its first fetch). Avoids stale slider values.
  useEffect(() => {
    setDraft(baseline);
  }, [baseline]);

  const dirty = !eq(draft, baseline);

  const onSlide = (which: keyof SubScoreDraft) => (next: number[]) => {
    if (!next.length) return;
    setDraft((cur) => rebalance(cur, which, next[0]));
  };

  const onReset = () => setDraft(baseline);

  const onSave = async () => {
    if (!dirty || saving || !canEdit || !accountState.account) return;
    setSaving(true);
    try {
      const { error } = await supabase!.rpc("replace_active_score_weights", {
        p_account_id: accountState.account.id,
        p_authenticity_w: draft.auth / 100,
        p_authority_w: draft.authority / 100,
        p_warmth_w: draft.warmth / 100,
        p_created_by: accountState.user?.email ?? "user",
      });
      if (error) throw error;
      invalidateActiveWeightVersion();
      toast.success(
        `Saved sub-score mix · ${draft.auth}/${draft.authority}/${draft.warmth}. Stale prospects will show a refresh banner.`,
      );
    } catch (err) {
      console.error("[Settings] sub-score mix save failed:", err);
      toast.error("Couldn't save sub-score mix. Check console for details.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid md:grid-cols-12 gap-10">
      <div className="md:col-span-4">
        <div className="label-eyebrow mb-3">Sub-score mix</div>
        <h1 className="text-4xl md:text-5xl font-light tracking-tight leading-[1.05] mb-6">
          How to combine.
        </h1>
        <p className="text-sm text-muted-foreground mb-6 leading-relaxed">
          The overall score is a weighted blend of three sub-scores —
          Authenticity, Authority, and Warmth. Saving creates a new weight
          version; previously-scored prospects show a refresh banner until
          they recompute.
        </p>
        <p className="text-[10px] text-muted-foreground mb-6 leading-relaxed">
          Sliders auto-rebalance to keep the total at <span className="text-mono">100%</span>.
        </p>
        <div className="flex items-center gap-3 flex-wrap">
          <button
            onClick={onSave}
            disabled={!dirty || saving || !canEdit}
            aria-busy={saving}
            className="border border-foreground bg-foreground text-background px-5 py-2.5 text-xs uppercase tracking-[0.16em] disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {saving ? "Saving…" : dirty ? "Save new version" : "No changes"}
          </button>
          {dirty && !saving && (
            <button
              onClick={onReset}
              className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground hover:text-foreground transition-colors"
            >
              Reset
            </button>
          )}
        </div>
        {!canEdit && (
          <div className="text-[10px] text-muted-foreground mt-6 leading-relaxed">
            {isDemo
              ? "Demo mode is read-only — sign in to edit the sub-score mix."
              : "Sign in to edit the sub-score mix."}
          </div>
        )}
      </div>

      <div className="md:col-span-8">
        <div className="border border-border">
          <SubScoreSlider
            label="Authenticity"
            value={draft.auth}
            onChange={onSlide("auth")}
            disabled={!canEdit || saving}
            dirty={draft.auth !== baseline.auth}
          />
          <SubScoreSlider
            label="Authority"
            value={draft.authority}
            onChange={onSlide("authority")}
            disabled={!canEdit || saving}
            dirty={draft.authority !== baseline.authority}
          />
          <SubScoreSlider
            label="Warmth"
            value={draft.warmth}
            onChange={onSlide("warmth")}
            disabled={!canEdit || saving}
            dirty={draft.warmth !== baseline.warmth}
            isLast
          />
          <div className="px-6 py-3 text-[10px] uppercase tracking-[0.16em] text-muted-foreground flex items-center justify-between border-t border-border">
            <span>Total</span>
            <span className="text-mono text-foreground">
              {draft.auth + draft.authority + draft.warmth}%
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};

const SubScoreSlider = ({
  label,
  value,
  onChange,
  disabled,
  dirty,
  isLast = false,
}: {
  label: string;
  value: number;
  onChange: (v: number[]) => void;
  disabled: boolean;
  dirty: boolean;
  isLast?: boolean;
}) => (
  <div
    className={`px-6 py-5 flex items-center gap-6 ${
      isLast ? "" : "border-b border-border/60"
    }`}
  >
    <div className="w-32 text-sm">{label}</div>
    <div className="flex-1">
      <Slider
        min={0}
        max={100}
        step={1}
        value={[value]}
        onValueChange={onChange}
        disabled={disabled}
      />
    </div>
    <div
      className={`w-16 text-right text-mono text-sm transition-colors ${
        dirty ? "text-accent" : "text-foreground"
      }`}
    >
      {value}%
    </div>
  </div>
);

export default Settings;
