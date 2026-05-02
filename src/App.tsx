import { lazy, Suspense, useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DemoBanner } from "@/components/DemoBanner";
import { DemoScript } from "@/components/DemoScript";
import { initDemoMode } from "@/store/graphStore";
import { DEMO_GRAPH_NODES, DEMO_EDGES } from "@/lib/demoData";
import { AccountProvider } from "@/contexts/AccountContext";
import { RequireAuth } from "@/components/RequireAuth";
import Index from "./pages/Index.tsx";
import NotFound from "./pages/NotFound.tsx";
// /discover is the hero route — keep it eager so the graph doesn't suspend
// behind a fallback flash on first paint.
import Discover from "./pages/Discover.tsx";

// Lazy-load secondary routes. /prospect/:id pulls react-flow (~80 KB) and
// /settings + /validate aren't on the landing critical path. /login pulls
// shadcn form primitives but isn't hit on every visit, so lazy too.
const Validate = lazy(() => import("./pages/Validate.tsx"));
const Settings = lazy(() => import("./pages/Settings.tsx"));
const ProspectDetail = lazy(() => import("./pages/ProspectDetail.tsx"));
const Login = lazy(() => import("./pages/Login.tsx"));
const OrgChart = lazy(() => import("./pages/OrgChart.tsx"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Refetching on focus thrashes the shared bulk-table caches.
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

const RouteFallback = () => (
  <div className="min-h-screen bg-background" aria-busy="true" />
);

const App = () => {
  // Demo-mode boot: when `?demo=true` is set, push the hardcoded demo graph
  // into the store on first mount. No-op in live mode (initDemoMode self-
  // gates on isDemoMode()). Per CONTRACTS.md Contract 5 §"Data loading
  // switch" — `loadGraphFromDemoData()` semantics, injected to avoid
  // demoData.ts ↔ graphStore.ts circular import.
  useEffect(() => {
    initDemoMode([...DEMO_GRAPH_NODES], [...DEMO_EDGES]);
  }, []);

  return (
  <ErrorBoundary>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <Toaster />
        <Sonner />
        <BrowserRouter
          future={{
            v7_startTransition: true,
            v7_relativeSplatPath: true,
          }}
        >
          {/* AccountProvider must wrap routes — useAccount() inside any
              page reads the resolved tenancy state. Demo mode short-
              circuits inside the provider, so demo routes never wait on
              Supabase Auth. */}
          <AccountProvider>
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                {/* Public routes — no auth required */}
                <Route path="/login" element={<Login />} />
                {/* Protected routes — RequireAuth redirects to /login when
                    account is null AND demo mode is off. Demo mode
                    (?demo=true) bypasses the guard inside the component. */}
                <Route path="/" element={<RequireAuth><Index /></RequireAuth>} />
                <Route path="/validate" element={<RequireAuth><Validate /></RequireAuth>} />
                <Route path="/discover" element={<RequireAuth><Discover /></RequireAuth>} />
                <Route path="/settings" element={<RequireAuth><Settings /></RequireAuth>} />
                <Route path="/prospect/:id" element={<RequireAuth><ProspectDetail /></RequireAuth>} />
                <Route path="/org/:companyId" element={<RequireAuth><OrgChart /></RequireAuth>} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Suspense>
            {/* Demo-mode chrome — both components self-gate via isDemoMode() */}
            <DemoBanner />
            <DemoScript />
          </AccountProvider>
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  </ErrorBoundary>
  );
};

export default App;
