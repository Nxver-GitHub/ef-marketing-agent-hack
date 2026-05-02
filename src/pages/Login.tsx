/**
 * Wave 6 M3 — minimal Supabase Auth sign-in / sign-up screen.
 *
 * Email + password only for v1. OAuth (Google / Microsoft) and magic
 * links are deferred. Hand-rolled rather than pulling in
 * @supabase/auth-ui-react — that package adds ~80KB and a Tailwind theme
 * fight; the surface here is small enough to write inline.
 */
import { useState, type FormEvent } from "react"
import { Link, Navigate, useLocation } from "react-router-dom"
import { supabase, HAS_REAL_SUPABASE } from "@/lib/supabase"
import { useAccount } from "@/contexts/AccountContext"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

type Mode = "signin" | "signup"

export default function Login() {
  const { account, loading, user } = useAccount()
  const location = useLocation()
  const [mode, setMode] = useState<Mode>("signin")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  // Already signed in + has an account → redirect to wherever they were headed
  if (!loading && account) {
    const redirectTo = (location.state as { from?: string } | null)?.from ?? "/"
    return <Navigate to={redirectTo} replace />
  }

  // Signed in but missing account → onboarding gap (unhandled in v1)
  const needsOnboarding = !loading && user && !account

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)
    setInfo(null)
    if (!HAS_REAL_SUPABASE || !supabase) {
      setError("Supabase isn't configured — check VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY.")
      return
    }
    setSubmitting(true)
    try {
      if (mode === "signin") {
        const { error: err } = await supabase.auth.signInWithPassword({ email, password })
        if (err) throw err
      } else {
        const { error: err } = await supabase.auth.signUp({ email, password })
        if (err) throw err
        setInfo(
          "Check your email for a confirmation link. After confirming, ask an admin to assign your account."
        )
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="min-h-screen bg-background text-foreground flex items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <Link to="/" className="block mb-8 text-center font-medium tracking-tight">
          CREDENCE<sup className="text-[8px] ml-0.5">®</sup>
        </Link>

        <h1 className="text-xl font-medium mb-6 text-center">
          {mode === "signin" ? "Sign in" : "Create account"}
        </h1>

        {needsOnboarding && (
          <div className="mb-4 text-xs border border-border bg-muted/40 p-3 leading-relaxed">
            You're signed in as <span className="font-mono">{user?.email}</span>, but no account
            is associated with this user yet. Ask an admin to add you, or sign up with a fresh
            email if this is a new workspace.
          </div>
        )}

        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <Label htmlFor="email" className="text-xs mb-1 block">
              Email
            </Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={submitting}
            />
          </div>
          <div>
            <Label htmlFor="password" className="text-xs mb-1 block">
              Password
            </Label>
            <Input
              id="password"
              type="password"
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              required
              minLength={mode === "signup" ? 8 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
            />
          </div>

          {error && (
            <p className="text-xs text-score-weak border-l-2 border-score-weak/40 pl-3 leading-relaxed">
              {error}
            </p>
          )}
          {info && (
            <p className="text-xs text-foreground/80 border-l-2 border-border pl-3 leading-relaxed">
              {info}
            </p>
          )}

          <Button type="submit" disabled={submitting} className="w-full">
            {submitting
              ? mode === "signin"
                ? "Signing in…"
                : "Creating account…"
              : mode === "signin"
              ? "Sign in"
              : "Create account"}
          </Button>
        </form>

        <div className="mt-6 text-center text-xs text-muted-foreground">
          {mode === "signin" ? (
            <>
              No account?{" "}
              <button
                type="button"
                onClick={() => {
                  setMode("signup")
                  setError(null)
                  setInfo(null)
                }}
                className="underline underline-offset-4 text-foreground"
              >
                Sign up
              </button>
            </>
          ) : (
            <>
              Already have an account?{" "}
              <button
                type="button"
                onClick={() => {
                  setMode("signin")
                  setError(null)
                  setInfo(null)
                }}
                className="underline underline-offset-4 text-foreground"
              >
                Sign in
              </button>
            </>
          )}
        </div>
      </div>
    </main>
  )
}
