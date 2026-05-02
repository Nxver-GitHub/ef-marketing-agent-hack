/**
 * Tests for SkillsChipCloud.
 *
 * Covers the contract from FRONTEND_TASKS:
 *   1. Renders up to topN
 *   2. "+N more" appears when over topN; click expands
 *   3. Empty array placeholder
 *   4. Default topN = 10
 *   5. Skills render as text (selectable)
 *   6. className passed through
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { SkillsChipCloud } from "./SkillsChipCloud";

afterEach(() => cleanup());

describe("SkillsChipCloud", () => {
  it("renders up to topN chips when topN < skills.length", () => {
    const skills = ["a", "b", "c", "d", "e"];
    render(<SkillsChipCloud skills={skills} topN={3} />);
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.getByText("c")).toBeInTheDocument();
    expect(screen.queryByText("d")).not.toBeInTheDocument();
    expect(screen.queryByText("e")).not.toBeInTheDocument();
  });

  it("shows +N more chip when skills.length > topN, and expands on click", () => {
    const skills = ["a", "b", "c", "d", "e"];
    render(<SkillsChipCloud skills={skills} topN={3} />);
    const moreBtn = screen.getByRole("button", { name: /show 2 more skills/i });
    expect(moreBtn).toHaveTextContent("+2 more");
    fireEvent.click(moreBtn);
    // After expanding, all skills are visible and "+N more" disappears
    expect(screen.getByText("d")).toBeInTheDocument();
    expect(screen.getByText("e")).toBeInTheDocument();
    expect(screen.queryByText(/\+2 more/)).not.toBeInTheDocument();
  });

  it("renders 'No skills listed.' on empty array", () => {
    render(<SkillsChipCloud skills={[]} />);
    expect(screen.getByText("No skills listed.")).toBeInTheDocument();
  });

  it("uses default topN = 10 when not provided", () => {
    const skills = Array.from({ length: 12 }, (_, i) => `s${i + 1}`);
    render(<SkillsChipCloud skills={skills} />);
    // First 10 visible, rest collapsed
    expect(screen.getByText("s1")).toBeInTheDocument();
    expect(screen.getByText("s10")).toBeInTheDocument();
    expect(screen.queryByText("s11")).not.toBeInTheDocument();
    expect(screen.queryByText("s12")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /show 2 more skills/i })).toBeInTheDocument();
  });

  it("renders skills as selectable text (select-text class present)", () => {
    render(<SkillsChipCloud skills={["typescript"]} />);
    const chip = screen.getByText("typescript");
    expect(chip.className).toContain("select-text");
  });

  it("passes className through to the container", () => {
    const { container } = render(
      <SkillsChipCloud skills={["a"]} className="custom-cls" />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("custom-cls");
  });

  it("does not show +N more when skills.length <= topN", () => {
    render(<SkillsChipCloud skills={["a", "b"]} topN={5} />);
    expect(screen.queryByText(/more/)).not.toBeInTheDocument();
  });

  it("passes className through on empty state", () => {
    const { container } = render(
      <SkillsChipCloud skills={[]} className="empty-cls" />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("empty-cls");
  });
});
