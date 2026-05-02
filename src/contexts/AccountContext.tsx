/**
 * Wave 6 M3 — AccountProvider + useAccount() hook.
 *
 * Subscribes to Supabase Auth state, resolves the signed-in user to their
 * Account (via `account_users` join), and exposes everything via the
 * `useAccount()` hook. Keeps `credenceHeaders.setActiveAccessToken` in
 * sync so backend fetches automatically carry the Bearer token.
 *
 * Per CONTRACTS.md Contract 9, demo mode short-circuits this entire flow:
 * when `?demo=true` is set, the provider renders the synthetic demo
 * account immediately (no Supabase calls) so the demo path never flashes
 * a "loading" state.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"
import type { Session } from "@supabase/supabase-js"
import { supabase, HAS_REAL_SUPABASE } from "@/lib/supabase"
import {
  fromSupabaseAccount,
  fromSupabaseUser,
  initialAccountState,
  isDemoAccount,
  type Account,
  type AccountState,
} from "@/lib/account"
import { setActiveAccessToken } from "@/lib/credenceHeaders"

// ── Context shape ───────────────────────────────────────────────────────────

interface AccountContextValue extends AccountState {
  /** Sign out + clear local state. Resolves once Supabase confirms. */
  signOut: () => Promise<void>
}

const AccountContext = createContext<AccountContextValue | null>(null)

// ── Provider ────────────────────────────────────────────────────────────────

export function AccountProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AccountState>(() => initialAccountState())

  useEffect(() => {
    // Demo mode is resolved synchronously by initialAccountState — nothing
    // to subscribe to. Bail before touching Supabase so demo-path renders
    // never trigger a network call.
    if (isDemoAccount(state) || !HAS_REAL_SUPABASE || !supabase) {
      return
    }

    let cancelled = false

    /**
     * Resolve a Supabase Session to (Account, User). Looks up
     * account_users + accounts for the signed-in user. Updates the
     * shared Bearer-token cache for backend fetches.
     */
    const resolve = async (session: Session | null) => {
      if (!session?.user) {
        setActiveAccessToken(null)
        if (!cancelled) {
          setState({ account: null, user: null, loading: false, error: null })
        }
        return
      }

      // Sync the cached token first — even if account lookup fails, any
      // backend fetch the user triggers should still authenticate.
      setActiveAccessToken(session.access_token)

      try {
        // 1. Resolve the user's account membership
        const { data: membership, error: memberErr } = await supabase!
          .from("account_users")
          .select("account_id")
          .eq("user_id", session.user.id)
          .limit(1)
          .maybeSingle()

        if (memberErr) throw memberErr

        if (!membership) {
          // User signed up but has no account yet — onboarding gap
          if (!cancelled) {
            setState({
              account: null,
              user: fromSupabaseUser(session.user),
              loading: false,
              error: "no_account",
            })
          }
          return
        }

        // 2. Pull the account row
        const { data: accountRow, error: acctErr } = await supabase!
          .from("accounts")
          .select("id, display_name, slug, plan_tier")
          .eq("id", membership.account_id)
          .single()

        if (acctErr) throw acctErr

        const account: Account = fromSupabaseAccount(accountRow)
        if (!cancelled) {
          setState({
            account,
            user: fromSupabaseUser(session.user),
            loading: false,
            error: null,
          })
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : "account_resolution_failed"
          setState({
            account: null,
            user: fromSupabaseUser(session.user),
            loading: false,
            error: message,
          })
        }
      }
    }

    // Initial resolution: read whatever session Supabase already has cached
    // (e.g., the user reloaded the page while logged in).
    supabase.auth.getSession().then(({ data }) => {
      if (!cancelled) void resolve(data.session)
    })

    // Subscribe to live changes — sign-in, sign-out, token refresh.
    const { data: subscription } = supabase.auth.onAuthStateChange((_event, session) => {
      if (cancelled) return
      void resolve(session)
    })

    return () => {
      cancelled = true
      subscription.subscription.unsubscribe()
    }
    // initialAccountState is stable; we only need to re-run if the
    // Supabase client identity changes, which it doesn't (module singleton).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const value = useMemo<AccountContextValue>(() => {
    return {
      ...state,
      signOut: async () => {
        if (!supabase) return
        await supabase.auth.signOut()
        setActiveAccessToken(null)
        // onAuthStateChange will fire and clear state; nothing to do here.
      },
    }
  }, [state])

  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>
}

// ── Hook ────────────────────────────────────────────────────────────────────

/**
 * Read the current account state. Throws if used outside an
 * `<AccountProvider>` so misconfigurations fail loud rather than
 * silently leak data via a default-empty state.
 */
export function useAccount(): AccountContextValue {
  const value = useContext(AccountContext)
  if (value === null) {
    throw new Error("useAccount must be used inside an <AccountProvider>")
  }
  return value
}
