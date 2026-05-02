/**
 * "Flag wrong reporting line" dialog — A4 UI half (V3_PT2.md L196-204).
 *
 * Surfaced as a button at the bottom of `PersonInspector`. Operator picks
 * one of 4 correction types; for the two that need a free-text correction
 * (`reports_to_other`, `team_wrong`) we render a labeled input. Submit
 * POSTs to `/orgchart/correction` (SwiftElk msg 146); toast on success/fail.
 *
 * The current implementation submits with `person_b_id = null` and
 * `edge_id = null` because we don't yet render org-chart edges in the UI
 * (org_reporting_edges has 0 rows pre-A0-apply). When the org chart UI
 * lands, callers can pass `defaultPersonBId` / `defaultEdgeId` to attach
 * the correction to a specific edge.
 */
import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";

import {
  submitOrgCorrection,
  type OrgCorrectionType,
  type OrgCorrectionRequest,
} from "@/lib/orgChartApi";

interface OrgCorrectionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The person being viewed — the "report" in a reports_to edge. */
  personAId: string;
  personAName: string;
  /** Optional — pre-filled when the dialog is opened from a specific edge. */
  defaultPersonBId?: string | null;
  defaultPersonBName?: string;
  defaultEdgeId?: string | null;
}

interface CorrectionOption {
  value: OrgCorrectionType;
  label: string;
  description: string;
  requiresCorrectValue: boolean;
  correctValueLabel?: string;
  correctValuePlaceholder?: string;
}

const OPTIONS: ReadonlyArray<CorrectionOption> = [
  {
    value: "not_reports_to",
    label: "They don't report to this person",
    description:
      "The inferred manager is wrong, but I don't know who they actually report to.",
    requiresCorrectValue: false,
  },
  {
    value: "reports_to_other",
    label: "They report to someone else",
    description:
      "The inferred manager is wrong; I know the actual manager's name.",
    requiresCorrectValue: true,
    correctValueLabel: "Actual manager (name or LinkedIn URL)",
    correctValuePlaceholder: "e.g. Jane Smith / https://linkedin.com/in/janes",
  },
  {
    value: "are_peers",
    label: "They're peers, not manager/report",
    description: "Both are at the same level on the org chart.",
    requiresCorrectValue: false,
  },
  {
    value: "team_wrong",
    label: "Their team / org is wrong",
    description: "The reporting line might be right, but the team label isn't.",
    requiresCorrectValue: true,
    correctValueLabel: "Correct team / org",
    correctValuePlaceholder: "e.g. Memory Architecture, Manufacturing Ops",
  },
];

export function OrgCorrectionDialog({
  open,
  onOpenChange,
  personAId,
  personAName,
  defaultPersonBId = null,
  defaultPersonBName,
  defaultEdgeId = null,
}: OrgCorrectionDialogProps) {
  const [correctionType, setCorrectionType] = useState<OrgCorrectionType | null>(
    null,
  );
  const [correctValue, setCorrectValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Reset state on close so a re-open starts fresh.
  useEffect(() => {
    if (!open) {
      setCorrectionType(null);
      setCorrectValue("");
      setSubmitting(false);
    }
  }, [open]);

  const selected = correctionType
    ? OPTIONS.find((o) => o.value === correctionType) ?? null
    : null;

  const canSubmit =
    correctionType !== null &&
    !submitting &&
    (!selected?.requiresCorrectValue || correctValue.trim().length > 0);

  async function handleSubmit() {
    if (!correctionType || !canSubmit) return;
    setSubmitting(true);

    const req: OrgCorrectionRequest = {
      person_a_id: personAId,
      correction_type: correctionType,
      person_b_id: defaultPersonBId,
      edge_id: defaultEdgeId,
      correct_value:
        selected?.requiresCorrectValue ? correctValue.trim() : null,
    };

    const result = await submitOrgCorrection(req);

    if (result.ok) {
      toast.success("Correction recorded", {
        description: `Thanks — your input on ${personAName} will help the inference engine.`,
      });
      onOpenChange(false);
    } else {
      toast.error("Couldn't save the correction", {
        description:
          result.message ||
          `${result.error} (status ${result.status}). The org-chart pipeline may not be live yet — try again after migration A0 applies.`,
      });
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Flag wrong reporting line</DialogTitle>
          <DialogDescription>
            Tell us what the inference engine got wrong about {personAName}
            {defaultPersonBName ? ` ↔ ${defaultPersonBName}` : ""}. Your
            correction is captured per-tenant and used to retrain the scoring
            weights.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 py-2">
          {OPTIONS.map((opt) => {
            const checked = correctionType === opt.value;
            return (
              <label
                key={opt.value}
                className={`flex items-start gap-2 p-2 cursor-pointer border ${
                  checked ? "border-foreground" : "border-border"
                }`}
              >
                <input
                  type="radio"
                  name="correction_type"
                  value={opt.value}
                  checked={checked}
                  onChange={() => setCorrectionType(opt.value)}
                  className="mt-1"
                />
                <div className="text-[12px] leading-relaxed">
                  <div className="font-medium">{opt.label}</div>
                  <div className="text-muted-foreground text-[11px]">
                    {opt.description}
                  </div>
                </div>
              </label>
            );
          })}

          {selected?.requiresCorrectValue && (
            <div className="space-y-1.5 pt-1">
              <Label htmlFor="correct-value" className="text-[11px]">
                {selected.correctValueLabel}
              </Label>
              <Input
                id="correct-value"
                value={correctValue}
                onChange={(e) => setCorrectValue(e.target.value)}
                placeholder={selected.correctValuePlaceholder}
                className="text-[12px]"
                autoFocus
              />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={handleSubmit}
            disabled={!canSubmit}
          >
            {submitting ? "Submitting…" : "Submit correction"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
