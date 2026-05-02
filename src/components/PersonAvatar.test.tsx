/**
 * Tests for PersonAvatar — initials, hash determinism, size variants.
 */
import { describe, it, expect, beforeEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import {
  PersonAvatar,
  computeInitials,
  avatarHash,
  avatarColorClass,
  AVATAR_PALETTE,
} from "./PersonAvatar"

beforeEach(() => cleanup())

// ── Pure helpers ───────────────────────────────────────────────────────────

describe("computeInitials", () => {
  it("uses first+last initials when both present", () => {
    expect(
      computeInitials({
        canonical_name: "Wei Chen",
        first_name: "Wei",
        last_name: "Chen",
      }),
    ).toBe("WC")
  })

  it("uses first initial alone when last is missing", () => {
    expect(
      computeInitials({
        canonical_name: "Madonna",
        first_name: "Madonna",
        last_name: null,
      }),
    ).toBe("M")
  })

  it("falls back to first 2 chars of canonical_name when no first/last", () => {
    expect(
      computeInitials({
        canonical_name: "Cher Bono",
        first_name: null,
        last_name: null,
      }),
    ).toBe("CH")
  })

  it("returns empty string for all-blank inputs", () => {
    expect(
      computeInitials({
        canonical_name: "",
        first_name: null,
        last_name: null,
      }),
    ).toBe("")
  })

  it("uppercases lowercase initials", () => {
    expect(
      computeInitials({
        canonical_name: "alice kim",
        first_name: "alice",
        last_name: "kim",
      }),
    ).toBe("AK")
  })
})

describe("avatarHash determinism", () => {
  it("returns identical hash for same name across calls", () => {
    expect(avatarHash("Wei Chen")).toBe(avatarHash("Wei Chen"))
  })

  it("returns different hash for different names", () => {
    expect(avatarHash("Wei Chen")).not.toBe(avatarHash("Wei Chenn"))
  })

  it("avatarColorClass returns one of the AVATAR_PALETTE entries", () => {
    const cls = avatarColorClass("Wei Chen")
    expect(AVATAR_PALETTE.includes(cls)).toBe(true)
  })

  it("same name → same color class on repeat calls", () => {
    const a = avatarColorClass("Sarah Kim")
    const b = avatarColorClass("Sarah Kim")
    expect(a).toBe(b)
  })
})

// ── Component render ───────────────────────────────────────────────────────

describe("PersonAvatar component", () => {
  it("renders initials inside the avatar", () => {
    render(
      <PersonAvatar
        person={{ canonical_name: "Wei Chen", first_name: "Wei", last_name: "Chen" }}
      />,
    )
    expect(screen.getByTestId("person-avatar").textContent).toBe("WC")
  })

  it("data-size reflects requested size", () => {
    render(
      <PersonAvatar
        person={{ canonical_name: "Wei Chen" }}
        size="xl"
      />,
    )
    expect(screen.getByTestId("person-avatar").getAttribute("data-size")).toBe(
      "xl",
    )
    expect(screen.getByTestId("person-avatar").className).toContain("w-16")
  })

  it("default size is md (w-10)", () => {
    render(<PersonAvatar person={{ canonical_name: "Wei Chen" }} />)
    expect(screen.getByTestId("person-avatar").className).toContain("w-10")
  })

  it("aria-label uses canonical_name", () => {
    render(<PersonAvatar person={{ canonical_name: "Wei Chen" }} />)
    expect(
      screen.getByLabelText("Wei Chen"),
    ).toBeInTheDocument()
  })

  it("custom className is appended", () => {
    render(
      <PersonAvatar
        person={{ canonical_name: "Wei Chen" }}
        className="my-extra-cls"
      />,
    )
    expect(screen.getByTestId("person-avatar").className).toContain(
      "my-extra-cls",
    )
  })

  it("showBorder=false omits ring class", () => {
    render(
      <PersonAvatar
        person={{ canonical_name: "Wei Chen" }}
        showBorder={false}
      />,
    )
    expect(screen.getByTestId("person-avatar").className).not.toContain(
      "ring-1",
    )
  })

  it("renders empty initials gracefully for all-blank person", () => {
    render(<PersonAvatar person={{ canonical_name: "" }} />)
    const avatar = screen.getByTestId("person-avatar")
    expect(avatar.textContent).toBe("")
    // Still has a color class from avatarColorClass("")
    expect(avatar.className).toContain("bg-")
  })
})
