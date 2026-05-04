/**
 * RequireAuth — currently a no-op pass-through.
 *
 * Auth gating is intentionally disabled so the demo is openly accessible.
 * Restore by reverting this file: the previous version redirected
 * unauthenticated visitors to /login.
 */
import { type ReactNode } from "react"

interface RequireAuthProps {
  children: ReactNode
}

export function RequireAuth({ children }: RequireAuthProps): JSX.Element {
  return <>{children}</>
}
