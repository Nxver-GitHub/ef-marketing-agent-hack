import { ReactNode } from "react";
import { TopBar } from "./TopBar";
import { Coordinates } from "./Coordinates";

export const PageShell = ({
  children,
  rightSlot,
}: {
  children: ReactNode;
  rightSlot?: ReactNode;
}) => (
  <div className="min-h-screen bg-background text-foreground">
    <TopBar />
    <main className="pt-12 min-h-screen relative">
      <div className="px-6 md:px-10 py-10 md:py-14 max-w-[1400px] mx-auto">{children}</div>
      <div className="fixed left-6 bottom-6 z-30 pointer-events-none">
        <Coordinates />
      </div>
      {rightSlot && (
        <div className="fixed right-6 bottom-6 z-30 text-mono text-[10px] text-muted-foreground">
          {rightSlot}
        </div>
      )}
    </main>
  </div>
);
