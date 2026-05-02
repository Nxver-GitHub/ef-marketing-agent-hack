/**
 * Onboarding — 4-stage progress UI shown after signup.
 *
 * Per CUSTOMER_ONBOARDING_PLAN.md §"Onboarding Progress UI" (lines 260-276)
 * + Wave C of LP delegation. Polls `GET /onboarding/status/:account_id`
 * every 3 seconds and renders stage indicators + progress bar.
 *
 * Once Stage 0 (identity) completes, the "Start exploring" CTA appears
 * and the rep can enter the app. The remaining stages keep running in
 * the background.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAccount } from "@/contexts/AccountContext";

const POLL_INTERVAL_MS = 3000;

interface OnboardingStatus {
  job_id: string | null;
  status: "pending" | "running" | "done" | "error";
  stage: "identity" | "company" | "team" | "connections" | "complete" | null;
  strategy: "all_employees" | "gtm_only" | null;
  progress: {
    total?: number;
    scraped?: number;
    matched?: number;
    new_persons?: number;
    cost?: { total_cents?: number };
    [k: string]: unknown;
  };
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
}

const STAGE_ORDER = ["identity", "company", "team", "connections"] as const;
type StageKey = (typeof STAGE_ORDER)[number];

const STAGE_LABEL: Record<StageKey | "complete", string> = {
  identity: "Your profile",
  company: "Your company",
  team: "Your team",
  connections: "Relationship graph",
  complete: "Done",
};

const STAGE_ETA: Record<StageKey, string> = {
  identity: "~2 min",
  company: "~5 min",
  team: "10–60 min",
  connections: "30–120 min",
};

function stageState(
  current: OnboardingStatus["stage"],
  target: StageKey,
): "done" | "running" | "pending" {
  if (!current) return "pending";
  if (current === "complete") return "done";
  const currentIdx = STAGE_ORDER.indexOf(current as StageKey);
  const targetIdx = STAGE_ORDER.indexOf(target);
  if (targetIdx < currentIdx) return "done";
  if (targetIdx === currentIdx) return "running";
  return "pending";
}

function StageRow({
  state,
  label,
  hint,
}: {
  state: "done" | "running" | "pending";
  label: string;
  hint?: string;
}): JSX.Element {
  const symbol = state === "done" ? "✓" : state === "running" ? "⟳" : "·";
  const colorClass =
    state === "done"
      ? "text-emerald-500"
      : state === "running"
        ? "text-foreground"
        : "text-muted-foreground";
  return (
    <div className="flex items-center justify-between py-2 border-b border-border/40 last:border-0">
      <div className="flex items-center gap-3">
        <span
          className={`inline-flex w-6 h-6 items-center justify-center text-mono ${colorClass} ${state === "running" ? "animate-spin" : ""}`}
          aria-hidden="true"
        >
          {symbol}
        </span>
        <span className={state === "pending" ? "text-muted-foreground" : "text-foreground"}>
          {label}
        </span>
      </div>
      {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
    </div>
  );
}

export default function Onboarding(): JSX.Element {
  const { account } = useAccount();
  const navigate = useNavigate();
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  const accountId = account?.id ?? null;

  useEffect(() => {
    if (!accountId) return;

    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const resp = await fetch(`/onboarding/status/${accountId}`);
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const data = (await resp.json()) as OnboardingStatus;
        if (cancelled) return;
        setStatus(data);
        setPollError(null);
        // Stop polling once the pipeline is fully done or errored
        if (data.status === "done" || data.status === "error") return;
      } catch (err) {
        if (cancelled) return;
        setPollError(err instanceof Error ? err.message : String(err));
      }
      if (!cancelled) timeoutId = setTimeout(poll, POLL_INTERVAL_MS);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    };
  }, [accountId]);

  const canEnterApp = useMemo(() => {
    if (!status) return false;
    // Stage 0 is the only blocking stage — rep can enter as soon as identity
    // is resolved AND the pipeline has advanced past it.
    if (!status.stage) return false;
    return status.stage !== "identity";
  }, [status]);

  const teamProgress = useMemo(() => {
    const total = status?.progress?.total;
    const scraped = status?.progress?.scraped;
    if (typeof total !== "number" || typeof scraped !== "number" || total === 0) return null;
    return { scraped, total, pct: Math.round((scraped / total) * 100) };
  }, [status?.progress]);

  if (!accountId) {
    return (
      <div className="min-h-screen flex items-center justify-center p-8">
        <p className="text-muted-foreground">Sign in to continue with onboarding.</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-8 bg-background">
      <div className="w-full max-w-md space-y-6">
        <div>
          <div className="label-eyebrow mb-2">Welcome to Credence</div>
          <h1 className="text-2xl font-light tracking-tight">
            Building your relationship graph
          </h1>
          <p className="text-sm text-muted-foreground mt-2">
            We're scanning public records to map every documented connection
            between your team and your prospects. You'll be unblocked in about
            two minutes; the rest happens in the background.
          </p>
        </div>

        <div className="border border-border rounded-md p-5 space-y-px">
          {STAGE_ORDER.map((s) => (
            <StageRow
              key={s}
              state={stageState(status?.stage ?? null, s)}
              label={STAGE_LABEL[s]}
              hint={STAGE_ETA[s]}
            />
          ))}
        </div>

        {teamProgress ? (
          <div>
            <div className="text-xs text-muted-foreground mb-1.5 flex justify-between">
              <span>{`Team scraped: ${teamProgress.scraped} of ${teamProgress.total}`}</span>
              <span>{`${teamProgress.pct}%`}</span>
            </div>
            <div className="h-2 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full bg-foreground transition-all"
                style={{ width: `${teamProgress.pct}%` }}
              />
            </div>
          </div>
        ) : null}

        {status?.error_message ? (
          <div className="border border-destructive bg-destructive/10 rounded-md p-3 text-sm">
            <div className="font-medium text-destructive">A stage failed</div>
            <div className="text-muted-foreground mt-1">{status.error_message}</div>
            <div className="text-xs text-muted-foreground mt-2">
              Onboarding will continue with whatever data could be gathered.
            </div>
          </div>
        ) : null}

        {pollError ? (
          <p className="text-xs text-destructive">
            Status check failed: {pollError}. Retrying…
          </p>
        ) : null}

        <button
          type="button"
          disabled={!canEnterApp}
          onClick={() => navigate("/discover")}
          className={`w-full py-3 px-4 rounded-md transition-colors ${
            canEnterApp
              ? "bg-foreground text-background hover:opacity-90"
              : "bg-muted text-muted-foreground cursor-not-allowed"
          }`}
        >
          {canEnterApp ? "Start exploring →" : "Resolving your profile…"}
        </button>
      </div>
    </div>
  );
}
