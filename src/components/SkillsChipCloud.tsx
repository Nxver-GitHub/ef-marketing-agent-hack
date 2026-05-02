/**
 * SkillsChipCloud — pure presentational chip cloud for a person's skills.
 *
 * Renders up to `topN` skills as inline chips. When `skills.length > topN`,
 * a "+N more" chip expands to reveal the remainder. Empty array renders a
 * neutral placeholder. Chips are presentational only — no click handler,
 * but text is selectable for copy/paste.
 *
 * Visual language matches the right-rail (NodeInspector): muted background,
 * border, rounded-full, tiny type. Pure component, no state outside the
 * expand toggle, no side effects.
 */
import { useState, type JSX } from "react";
import { cn } from "@/lib/utils";

export interface SkillsChipCloudProps {
  /** Skills to render. Order is preserved; caller sorts upstream if needed. */
  skills: string[];
  /** Maximum chips before collapsing into "+N more". Default 10. */
  topN?: number;
  /** Optional className passthrough — composes with the default container. */
  className?: string;
}

const DEFAULT_TOP_N = 10;

const CHIP_CLASS =
  "rounded-full text-xs px-2 py-0.5 border bg-muted hover:border-primary select-text";

export function SkillsChipCloud(props: SkillsChipCloudProps): JSX.Element {
  const { skills, topN = DEFAULT_TOP_N, className } = props;
  const [expanded, setExpanded] = useState(false);

  if (!skills || skills.length === 0) {
    return (
      <div className={cn("text-xs text-muted-foreground", className)}>
        No skills listed.
      </div>
    );
  }

  const overflow = skills.length - topN;
  const visible = expanded || overflow <= 0 ? skills : skills.slice(0, topN);

  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      {visible.map((skill, i) => (
        <span key={`${skill}|${i}`} className={CHIP_CLASS}>
          {skill}
        </span>
      ))}
      {overflow > 0 && !expanded && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className={cn(CHIP_CLASS, "cursor-pointer text-muted-foreground")}
          aria-label={`Show ${overflow} more skills`}
        >
          +{overflow} more
        </button>
      )}
    </div>
  );
}
