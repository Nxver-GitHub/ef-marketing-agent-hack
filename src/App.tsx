import { lazy, Suspense } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import Index from "./pages/Index.tsx";
import NotFound from "./pages/NotFound.tsx";
// /discover is the hero route — keep it eager so the graph doesn't suspend
// behind a fallback flash on first paint.
import Discover from "./pages/Discover.tsx";

// Lazy-load secondary routes. /prospect/:id pulls react-flow (~80 KB) and
// /settings + /validate aren't on the landing critical path.
const Validate = lazy(() => import("./pages/Validate.tsx"));
const Settings = lazy(() => import("./pages/Settings.tsx"));
const ProspectDetail = lazy(() => import("./pages/ProspectDetail.tsx"));

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

const App = () => (
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
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route path="/" element={<Index />} />
              <Route path="/validate" element={<Validate />} />
              <Route path="/discover" element={<Discover />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/prospect/:id" element={<ProspectDetail />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  </ErrorBoundary>
);

export default App;
