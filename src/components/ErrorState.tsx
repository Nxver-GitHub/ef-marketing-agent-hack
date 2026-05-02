/**
 * ErrorState — friendly error placeholder with optional retry button.
 *
 * Used by Companies / OrgChart / People pages when supabase / fetch fails.
 * Pure presentational. Caller passes the error + optional retry callback.
 */
import type { JSX } from "react"
import { cn } from "@/lib/utils"

export interface ErrorStateProps {
  /** Either an Error instance or a pre-formatted string message. */
  error: Error | string
  /** Optional retry callback. When provided, a "Try again" button renders. */
  retry?: () => void
  /** Override the default top-line title. */
  title?: string
  className?: string
}

/** Pure: extract a stable string message from an Error or string. */
export function errorMessage(err: Error | string | null | undefined): string {
  if (err == null) return ""
  if (typeof err === "string") return err
  if (err instanceof Error) return err.message || String(err)
  // Defensive: someone passed a non-string non-Error (e.g., a Response).
  try {
    return String(err)
  } catch {
    return "Unknown error"
  }
}

export function ErrorState({
  error,
  retry,
  title = "Something went wrong",
  className,
}: ErrorStateProps): JSX.Element {
  const msg = errorMessage(error)
  return (
    <div
      role="alert"
      className={cn(
        "min-h-screen bg-background p-6 flex items-center justify-center",
        className,
      )}
      data-testid="error-state"
    >
      <div className="max-w-lg w-full border border-red-500/40 bg-red-500/5 p-6 space-y-3">
        <h2
          className="text-base font-semibold text-red-300"
          data-testid="error-state-title"
        >
          {title}
        </h2>
        {msg && (
          <p
            className="text-sm text-muted-foreground break-words"
            data-testid="error-state-message"
          >
            {msg}
          </p>
        )}
        {retry && (
          <button
            type="button"
            onClick={retry}
            className="border border-border px-3 py-1 text-sm hover:border-accent transition-colors"
            data-testid="error-state-retry"
          >
            Try again
          </button>
        )}
      </div>
    </div>
  )
}
