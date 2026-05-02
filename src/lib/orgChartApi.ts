/**
 * Client wrapper for the org-chart correction endpoints.
 *
 * Backend route shipped by SwiftElk in msg 146:
 *   POST /orgchart/correction
 *   body: { person_a_id, correction_type, person_b_id?, edge_id?, correct_value? }
 *   returns 200 { correction_id } | 400 invalid_correction | 404 edge_not_found
 *           | 502 correction_persist_failed
 *
 * The backend route is live now even though the A0 schema migration may not
 * yet be applied to live Supabase (drafted in msg 135, awaiting LP apply).
 * Calls before apply will return 502 — the dialog UX surfaces the message
 * verbatim so the operator can route it to the right person.
 *
 * `getCredenceHeaders()` attaches `X-Credence-Demo: true` in demo mode and
 * `Authorization: Bearer <token>` once the AccountContext is wired (M3).
 */
import { getCredenceHeaders } from "./credenceHeaders";

const API_URL =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

/** The 4-value correction-type keyspace from V3_PT2.md L196-204 + SwiftElk msg 146. */
export type OrgCorrectionType =
  | "not_reports_to"
  | "reports_to_other"
  | "are_peers"
  | "team_wrong";

export interface OrgCorrectionRequest {
  /** Required — the person being viewed (the "report" in a reports_to edge). */
  person_a_id: string;
  /** Required — what's wrong with the inferred relationship. */
  correction_type: OrgCorrectionType;
  /** Optional — the inferred manager. Null when the user doesn't know who's correct. */
  person_b_id?: string | null;
  /** Optional — the org_reporting_edges row this correction targets. */
  edge_id?: string | null;
  /**
   * Required for `reports_to_other` (who they actually report to) and
   * `team_wrong` (what team they're actually on). Optional for the other 2.
   */
  correct_value?: string | null;
}

export interface OrgCorrectionSuccess {
  ok: true;
  correction_id: string;
}

export interface OrgCorrectionError {
  ok: false;
  status: number;
  error: string;
  /** Human-readable detail when the server provided one. */
  message?: string;
}

export type OrgCorrectionResult = OrgCorrectionSuccess | OrgCorrectionError;

/**
 * POST a correction. Defensive — never throws; returns a discriminated
 * result the caller can branch on. Network failures collapse to a 0-status
 * error so the UI can show "Couldn't reach the server" without a crash.
 */
export async function submitOrgCorrection(
  req: OrgCorrectionRequest,
): Promise<OrgCorrectionResult> {
  let resp: Response;
  try {
    resp = await fetch(`${API_URL}/orgchart/correction`, {
      method: "POST",
      headers: { "content-type": "application/json", ...getCredenceHeaders() },
      body: JSON.stringify(req),
    });
  } catch (err) {
    return {
      ok: false,
      status: 0,
      error: "network_error",
      message: err instanceof Error ? err.message : "request failed",
    };
  }

  let body: unknown = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }

  if (!resp.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : null;
    const errKey =
      detail && typeof detail === "object" && "error" in detail
        ? String((detail as { error: unknown }).error)
        : `http_${resp.status}`;
    return {
      ok: false,
      status: resp.status,
      error: errKey,
      message:
        detail && typeof detail === "object" && "message" in detail
          ? String((detail as { message: unknown }).message)
          : undefined,
    };
  }

  const correctionId =
    body && typeof body === "object" && "correction_id" in body
      ? String((body as { correction_id: unknown }).correction_id)
      : "";
  return { ok: true, correction_id: correctionId };
}
