import { Link, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { PageShell } from "@/components/PageShell";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

const NotFound = () => {
  const location = useLocation();
  useDocumentTitle("404");

  useEffect(() => {
    console.error("404 — no route matched:", location.pathname);
  }, [location.pathname]);

  return (
    <PageShell>
      <div className="grid md:grid-cols-12 gap-10 min-h-[60vh] items-center">
        <div className="md:col-span-7">
          <div className="label-eyebrow mb-3">Error · 404</div>
          <h1 className="text-5xl md:text-7xl font-light tracking-tight leading-[1.02] mb-6">
            That route isn't on the map.
          </h1>
          <p className="text-sm text-muted-foreground max-w-md leading-relaxed">
            <span className="text-mono text-foreground">{location.pathname}</span>{" "}
            doesn't resolve to any view. Jump to the pipeline graph, the search
            form, or the home page below.
          </p>
        </div>

        <div className="md:col-span-5 flex flex-col gap-px">
          <Link
            to="/discover"
            className="group border border-border p-6 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-3">Pipeline</div>
            <div className="text-xl font-light tracking-tight mb-1">
              Browse the prospect graph →
            </div>
            <div className="text-xs text-muted-foreground">
              Force-directed view of every scored person, company, and city in
              the network.
            </div>
          </Link>
          <Link
            to="/validate"
            className="group border border-border p-6 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-3">Validate</div>
            <div className="text-xl font-light tracking-tight mb-1">
              Search for a specific lead →
            </div>
            <div className="text-xs text-muted-foreground">
              Look up a person by name + company + role; rank against existing
              candidates.
            </div>
          </Link>
          <Link
            to="/"
            className="border border-border p-4 text-xs text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
          >
            ← Back to home
          </Link>
        </div>
      </div>
    </PageShell>
  );
};

export default NotFound;
