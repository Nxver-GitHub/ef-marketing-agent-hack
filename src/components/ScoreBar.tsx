export const scoreColor = (n: number) => {
  if (n >= 70) return "hsl(var(--success))";
  if (n >= 45) return "hsl(var(--warning))";
  return "hsl(var(--danger))";
};

export const ScoreBar = ({
  label,
  value,
  hint,
}: {
  label: string;
  value: number;
  hint?: string;
}) => (
  <div className="space-y-1.5">
    <div className="flex items-baseline justify-between">
      <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">{label}</div>
      <div className="text-mono text-sm">{value.toFixed(1)}</div>
    </div>
    <div className="h-px w-full bg-border relative overflow-hidden">
      <div
        className="absolute inset-y-0 left-0 h-full"
        style={{ width: `${value}%`, background: scoreColor(value), height: "2px", marginTop: "-0.5px" }}
      />
    </div>
    {hint && <div className="text-[10px] text-muted-foreground">{hint}</div>}
  </div>
);

export const BigScore = ({ value }: { value: number }) => (
  <div className="flex items-baseline gap-3">
    <div
      className="text-[120px] md:text-[180px] leading-none font-light tracking-tighter"
      style={{ color: scoreColor(value) }}
    >
      {Math.round(value)}
    </div>
    <div className="text-mono text-xs text-muted-foreground">
      <div>/100</div>
      <div className="mt-1 uppercase">Overall</div>
    </div>
  </div>
);
