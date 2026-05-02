/**
 * Tests for `PersonProfileCard`.
 *
 * Covers the contract enumerated in the spec (17 cases): identity rendering,
 * graceful collapse of optional sections, badge gating, flag emoji edge cases,
 * count formatting, email status pills, LinkedIn URL slug extraction, and
 * avatar initials fallback. Pure presentational tests — no mocks, no router,
 * no data layer.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import {
  PersonProfileCard,
  flagEmoji,
  computeInitials,
  linkedinSlug,
  type PersonProfileCardProps,
} from "./PersonProfileCard";

// ── Fixture builder ─────────────────────────────────────────────────────────

function makePerson(
  overrides: Partial<PersonProfileCardProps["person"]> = {},
): PersonProfileCardProps["person"] {
  return {
    canonical_name: "Jensen Huang",
    first_name: "Jensen",
    last_name: "Huang",
    ...overrides,
  };
}

afterEach(() => cleanup());

// ── 1. Identity rendering ───────────────────────────────────────────────────

describe("PersonProfileCard — identity", () => {
  it("renders canonical name and headline", () => {
    render(
      <PersonProfileCard
        person={makePerson({ headline: "Founder & CEO at NVIDIA" })}
      />,
    );
    expect(screen.getByText("Jensen Huang")).toBeInTheDocument();
    expect(screen.getByTestId("profile-headline")).toHaveTextContent(
      "Founder & CEO at NVIDIA",
    );
  });

  it("renders flag emoji from country_code (US → 🇺🇸)", () => {
    render(<PersonProfileCard person={makePerson({ country_code: "US" })} />);
    const loc = screen.getByTestId("profile-location");
    expect(loc.textContent).toContain("🇺🇸");
  });

  it("renders location_text when present", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          country_code: "US",
          location_text: "Santa Clara, CA",
        })}
      />,
    );
    expect(screen.getByTestId("profile-location")).toHaveTextContent(
      "Santa Clara, CA",
    );
  });

  it("skips location row when both location_text and country_code are missing", () => {
    render(<PersonProfileCard person={makePerson()} />);
    expect(screen.queryByTestId("profile-location")).toBeNull();
  });
});

// ── 2. flagEmoji helper ─────────────────────────────────────────────────────

describe("flagEmoji", () => {
  it("returns empty string for null/undefined/empty input", () => {
    expect(flagEmoji(null)).toBe("");
    expect(flagEmoji(undefined)).toBe("");
    expect(flagEmoji("")).toBe("");
    expect(flagEmoji("   ")).toBe("");
  });

  it("normalises lowercase to flag emoji", () => {
    expect(flagEmoji("us")).toBe(flagEmoji("US"));
    expect(flagEmoji("us")).not.toBe("");
  });

  it("returns empty string for non-ASCII or non-letter input", () => {
    expect(flagEmoji("U1")).toBe("");
    expect(flagEmoji("12")).toBe("");
    expect(flagEmoji("日本")).toBe("");
    expect(flagEmoji("USA")).toBe("");
    expect(flagEmoji("U")).toBe("");
  });
});

// ── 3. Badges ───────────────────────────────────────────────────────────────

describe("PersonProfileCard — badges", () => {
  it("renders Premium badge only when premium=true", () => {
    const { rerender } = render(
      <PersonProfileCard person={makePerson({ premium: false })} />,
    );
    expect(screen.queryByTestId("badge-premium")).toBeNull();

    rerender(<PersonProfileCard person={makePerson({ premium: true })} />);
    expect(screen.getByTestId("badge-premium")).toBeInTheDocument();
  });

  it("renders Verified badge only when verified=true", () => {
    const { rerender } = render(
      <PersonProfileCard person={makePerson({ verified: null })} />,
    );
    expect(screen.queryByTestId("badge-verified")).toBeNull();

    rerender(<PersonProfileCard person={makePerson({ verified: true })} />);
    expect(screen.getByTestId("badge-verified")).toBeInTheDocument();
  });

  it("renders Open to Work badge only when open_to_work=true", () => {
    const { rerender } = render(
      <PersonProfileCard person={makePerson({ open_to_work: false })} />,
    );
    expect(screen.queryByTestId("badge-open-to-work")).toBeNull();

    rerender(<PersonProfileCard person={makePerson({ open_to_work: true })} />);
    expect(screen.getByTestId("badge-open-to-work")).toBeInTheDocument();
  });

  it("renders Hiring badge only when hiring=true", () => {
    const { rerender } = render(
      <PersonProfileCard person={makePerson({ hiring: undefined })} />,
    );
    expect(screen.queryByTestId("badge-hiring")).toBeNull();

    rerender(<PersonProfileCard person={makePerson({ hiring: true })} />);
    expect(screen.getByTestId("badge-hiring")).toBeInTheDocument();
  });

  it("renders all four badges together when all flags are true", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          premium: true,
          verified: true,
          open_to_work: true,
          hiring: true,
        })}
      />,
    );
    expect(screen.getByTestId("badge-premium")).toBeInTheDocument();
    expect(screen.getByTestId("badge-verified")).toBeInTheDocument();
    expect(screen.getByTestId("badge-open-to-work")).toBeInTheDocument();
    expect(screen.getByTestId("badge-hiring")).toBeInTheDocument();
  });
});

// ── 4. Reach row ────────────────────────────────────────────────────────────

describe("PersonProfileCard — reach", () => {
  it("formats connections with comma separator", () => {
    render(
      <PersonProfileCard
        person={makePerson({ connections_count: 1582 })}
      />,
    );
    expect(screen.getByTestId("reach-connections")).toHaveTextContent("1,582");
  });

  it("formats large follower counts (1234567 → 1,234,567)", () => {
    render(
      <PersonProfileCard
        person={makePerson({ followers_count: 1234567 })}
      />,
    );
    expect(screen.getByTestId("reach-followers")).toHaveTextContent(
      "1,234,567",
    );
  });

  it("renders registered_at as Mon YYYY", () => {
    render(
      <PersonProfileCard
        person={makePerson({ registered_at: "2014-02-15T00:00:00Z" })}
      />,
    );
    expect(screen.getByTestId("reach-registered")).toHaveTextContent("Feb 2014");
  });

  it("omits the entire reach row when all three fields are null", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          connections_count: null,
          followers_count: null,
          registered_at: null,
        })}
      />,
    );
    expect(screen.queryByTestId("profile-reach")).toBeNull();
  });

  it("renders only the cells that have data", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          connections_count: 500,
          followers_count: null,
          registered_at: null,
        })}
      />,
    );
    expect(screen.getByTestId("reach-connections")).toBeInTheDocument();
    expect(screen.queryByTestId("reach-followers")).toBeNull();
    expect(screen.queryByTestId("reach-registered")).toBeNull();
  });
});

// ── 5. Contact row ──────────────────────────────────────────────────────────

describe("PersonProfileCard — contact", () => {
  it("renders email status pill with verified styling", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          email: "j@nvidia.com",
          email_status: "verified",
        })}
      />,
    );
    const pill = screen.getByTestId("email-pill-verified");
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveTextContent(/verified email/i);
  });

  it("renders email status pill with guessed styling", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          email: "j@nvidia.com",
          email_status: "guessed",
        })}
      />,
    );
    const pill = screen.getByTestId("email-pill-guessed");
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveTextContent(/guessed/i);
  });

  it("renders email status pill with unverified styling", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          email: "j@nvidia.com",
          email_status: "unverified",
        })}
      />,
    );
    expect(screen.getByTestId("email-pill-unverified")).toBeInTheDocument();
  });

  it("omits the email row entirely when email is null", () => {
    render(<PersonProfileCard person={makePerson({ email: null })} />);
    expect(screen.queryByTestId("profile-email")).toBeNull();
  });

  it("omits the email row when email_status is 'unavailable'", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          email: "j@nvidia.com",
          email_status: "unavailable",
        })}
      />,
    );
    expect(screen.queryByTestId("profile-email")).toBeNull();
  });

  it("renders LinkedIn link with slug extracted and target=_blank", () => {
    render(
      <PersonProfileCard
        person={makePerson({
          linkedin_url: "https://linkedin.com/in/jenhsunhuang",
        })}
      />,
    );
    const link = screen.getByTestId("linkedin-link") as HTMLAnchorElement;
    expect(link).toHaveAttribute("href", "https://linkedin.com/in/jenhsunhuang");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link.textContent).toContain("jenhsunhuang");
  });

  it("omits the contact row when both email and linkedin_url are missing", () => {
    render(<PersonProfileCard person={makePerson()} />);
    expect(screen.queryByTestId("profile-contact")).toBeNull();
  });
});

// ── 6. Avatar initials ──────────────────────────────────────────────────────

describe("PersonProfileCard — avatar", () => {
  it("shows initials from first/last name", () => {
    render(
      <PersonProfileCard
        person={makePerson({ first_name: "Jensen", last_name: "Huang" })}
      />,
    );
    expect(screen.getByTestId("profile-avatar")).toHaveTextContent("JH");
  });

  it("falls back to first 2 chars of canonical_name when first/last are missing", () => {
    render(
      <PersonProfileCard
        person={{
          canonical_name: "Cher",
          first_name: null,
          last_name: null,
        }}
      />,
    );
    expect(screen.getByTestId("profile-avatar")).toHaveTextContent("CH");
  });

  it("computeInitials respects the documented fallback chain", () => {
    expect(computeInitials({ canonical_name: "Test User" })).toBe("TE");
    expect(
      computeInitials({
        canonical_name: "ignored",
        first_name: "Ada",
        last_name: "Lovelace",
      }),
    ).toBe("AL");
    expect(
      computeInitials({
        canonical_name: "ignored",
        first_name: "Ada",
        last_name: null,
      }),
    ).toBe("A");
  });
});

// ── 7. linkedinSlug helper ──────────────────────────────────────────────────

describe("linkedinSlug", () => {
  it("extracts slug from a standard /in/ url", () => {
    expect(linkedinSlug("https://linkedin.com/in/jenhsunhuang")).toBe(
      "jenhsunhuang",
    );
  });

  it("strips trailing slash and query string", () => {
    expect(
      linkedinSlug("https://www.linkedin.com/in/ada-lovelace/?trk=foo"),
    ).toBe("ada-lovelace");
  });
});
