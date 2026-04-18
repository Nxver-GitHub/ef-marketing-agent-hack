export const Coordinates = ({
  className = "",
  lines = ["N 37° 46′ 30″", "W 122° 25′ 09″"],
}: {
  className?: string;
  lines?: string[];
}) => (
  <div className={`text-mono text-[10px] text-muted-foreground leading-tight ${className}`}>
    {lines.map((l) => (
      <div key={l}>{l}</div>
    ))}
  </div>
);
