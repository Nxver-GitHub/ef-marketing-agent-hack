import { Link } from "react-router-dom";
import { PageShell } from "@/components/PageShell";
import { HeroMark } from "@/components/HeroMark";

const Index = () => {
  return (
    <PageShell rightSlot={<div>v0.1 — hackathon build</div>}>
      <div className="grid md:grid-cols-12 gap-10 min-h-[70vh]">
        <div className="md:col-span-7 flex flex-col justify-between">
          <HeroMark className="w-full max-w-[640px]" />
          <div className="mt-10 max-w-2xl">
            <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground mb-3">
              (Working Worldwide)
            </p>
            <h1 className="text-4xl md:text-6xl font-light leading-[1.05] tracking-tight">
              Credence<sup className="text-xs ml-1 align-super">®</sup> is a
              <br />
              trust-and-fit scoring tool
              <br />
              for B2B prospects.
            </h1>
          </div>
        </div>

        <div className="md:col-span-5 flex flex-col justify-end gap-px">
          <Link
            to="/validate"
            className="group block border border-border p-8 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-6">Flow 01</div>
            <div className="text-2xl md:text-3xl font-light tracking-tight mb-2">
              Validate a person.
            </div>
            <div className="text-sm text-muted-foreground mb-10">
              Enter a name, company, role and industry. Get a transparent trust-and-fit score with
              every contributing signal exposed.
            </div>
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /validate</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>

          <Link
            to="/discover"
            className="group block border border-border border-t-0 p-8 hover:bg-secondary transition-colors"
          >
            <div className="label-eyebrow mb-6">Flow 02</div>
            <div className="text-2xl md:text-3xl font-light tracking-tight mb-2">
              Find ICP matches.
            </div>
            <div className="text-sm text-muted-foreground mb-10">
              Define an ideal-customer profile. Get a ranked list of prospects, each with its own
              full breakdown.
            </div>
            <div className="flex items-center justify-between text-xs text-mono">
              <span className="text-muted-foreground">→ /discover</span>
              <span className="opacity-0 group-hover:opacity-100 transition-opacity">↗</span>
            </div>
          </Link>
        </div>
      </div>
    </PageShell>
  );
};

export default Index;
