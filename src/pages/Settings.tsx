import { useState } from "react";
import { toast } from "sonner";
import { PageShell } from "@/components/PageShell";
import { useWeights, useProspects, db } from "@/lib/db";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

const Settings = () => {
  useDocumentTitle("Settings");
  const weights = useWeights();
  const prospects = useProspects();
  const [draft, setDraft] = useState<Record<string, [number, number, number]>>({});
  const [saving, setSaving] = useState(false);
  const dirty = Object.keys(draft).length;

  const get = (signal_type: string, idx: 0 | 1 | 2, fallback: number) =>
    draft[signal_type]?.[idx] ?? fallback;

  const setVal = (signal_type: string, idx: 0 | 1 | 2, v: number, w: any) => {
    const cur = draft[signal_type] ?? [
      w.authenticity_weight,
      w.authority_weight,
      w.warmth_weight,
    ];
    const next: [number, number, number] = [...cur] as any;
    next[idx] = v;
    setDraft({ ...draft, [signal_type]: next });
  };

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
          <p className="text-sm text-muted-foreground mb-6">
            Tune how each signal contributes to the three sub-scores. Saving recomputes every
            prospect immediately. Scoring code never hardcodes weights — they live here.
          </p>
          <div className="flex items-center gap-3">
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
              <span className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
                {dirty} unsaved
              </span>
            )}
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
            {weights.map((w) => (
              <div
                key={w._id}
                className="grid grid-cols-12 items-center px-4 py-3 border-b border-border/60 last:border-0"
              >
                <div className="col-span-5 text-sm text-mono">{w.signal_type}</div>
                <WeightInput
                  v={get(w.signal_type, 0, w.authenticity_weight)}
                  onChange={(v) => setVal(w.signal_type, 0, v, w)}
                />
                <WeightInput
                  v={get(w.signal_type, 1, w.authority_weight)}
                  onChange={(v) => setVal(w.signal_type, 1, v, w)}
                />
                <WeightInput
                  v={get(w.signal_type, 2, w.warmth_weight)}
                  onChange={(v) => setVal(w.signal_type, 2, v, w)}
                  cols={3}
                />
              </div>
            ))}
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
}: {
  v: number;
  onChange: (v: number) => void;
  cols?: 2 | 3;
}) => (
  <div className={`${cols === 3 ? "col-span-3" : "col-span-2"} flex justify-end`}>
    <input
      type="number"
      step={0.1}
      min={0}
      max={2}
      value={v}
      onChange={(e) => onChange(Number(e.target.value))}
      className="w-20 text-right bg-transparent border border-border px-2 py-1 text-mono text-xs focus:outline-none focus:border-accent"
    />
  </div>
);

export default Settings;
