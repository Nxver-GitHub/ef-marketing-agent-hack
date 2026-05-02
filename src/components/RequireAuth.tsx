/**
 * RequireAuth — route guard that redirects unauthenticated visitors to /login.
 *
 * Wraps protected routes in App.tsx. Lets demo-mode (`?demo=true`) and
 * authenticated sessions through; everyone else redirects to /login,
 * preserving the originally-requested path so post-login navigation can
 * land them where they wanted.
 *
 * RLS already prevents anon-key reads from leaking other tenants' rows
 * (msg 244 audit confirmed all 14 tenant tables have rowsecurity=t).
 * This guard is the UX layer on top: an unauthenticated visit shouldn't
 * render an empty-but-broken page — it should redirect to login.
 */
import { type ReactNode } from "react"
import { Navigate, useLocation } from "react-router-dom"
import { useAccount } from "@/contexts/AccountContext"
import { isDemoMode } from "@/store/graphStore"

interface RequireAuthProps {
  children: ReactNode
}

export function RequireAuth({ children }: RequireAuthProps): JSX.Element {
  const { account, loading } = useAccount()
  const location = useLocation()

  // Demo mode (?demo=true) bypasses auth — the demo data is hard-coded
  // and doesn't touch real tenants. Per CONTRACTS.md Contract 5.
  if (isDemoMode()) {
    return <>{children}</>
  }

  // Don't redirect while AccountProvider is still resolving the session.
  // A brief loading state avoids the bounce-to-login flash on hard refresh
  // for already-authenticated users.
  if (loading) {
    return <div className="min-h-screen bg-background" aria-busy="true" />
  }

  // No account → redirect to /login. Preserve the requested URL so
  // Login.tsx can push back here on success (read via location.state.from).
  if (!account) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  return <>{children}</>
}
