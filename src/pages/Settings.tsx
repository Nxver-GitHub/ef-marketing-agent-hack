import { useState } from "react";
import { toast } from "sonner";
import { PageShell } from "@/components/PageShell";
import { useWeights, useProspects, db, type SignalWeight } from "@/lib/db";
import { useDocumentTitle } from "@/lib/useDocumentTitle";
import { HAS_REAL_SUPABASE } from "@/lib/supabase";

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
            <span className="text-mono">0.4 · auth + 0.4 · authority + 0.2 · warmth</span>.
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

export default Settings;
