/**
 * Studio—BA-style hero mark: white disc with overlaid vertical hairlines.
 * Pure SVG, scales to container.
 */
export const HeroMark = ({ className = "" }: { className?: string }) => {
  const lines = 56;
  return (
    <svg viewBox="0 0 800 480" className={className} preserveAspectRatio="xMidYMid meet">
      <defs>
        <clipPath id="bar-clip">
          <rect x="120" y="40" width="560" height="400" />
        </clipPath>
      </defs>
      {/* disc */}
      <ellipse cx="320" cy="240" rx="180" ry="180" fill="hsl(var(--foreground))" />
      {/* hairlines on top */}
      <g clipPath="url(#bar-clip)" stroke="hsl(var(--background))" strokeWidth="1.2">
        {Array.from({ length: lines }).map((_, i) => {
          const x = 120 + (i * 560) / (lines - 1);
          return <line key={i} x1={x} y1={40} x2={x} y2={440} />;
        })}
      </g>
      {/* hairlines outside disc — drawn in fg color */}
      <g stroke="hsl(var(--foreground))" strokeWidth="1.2" opacity="0.85">
        {Array.from({ length: lines }).map((_, i) => {
          const x = 120 + (i * 560) / (lines - 1);
          // skip lines that fall inside the ellipse footprint roughly
          const inDisc = x > 320 - 180 && x < 320 + 180;
          if (inDisc) return null;
          return <line key={i} x1={x} y1={40} x2={x} y2={440} />;
        })}
      </g>
    </svg>
  );
};
