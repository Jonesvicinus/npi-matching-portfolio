---
colors:
  brand: "#008BB3"
  brand-600: "#007296"
  brand-700: "#005A78"
  brand-tint-10: "rgba(0,139,179,.10)"
  brand-tint-20: "rgba(0,139,179,.20)"
  secondary: "#203B56"
  secondary-80: "#2E5274"
  tertiary: "#CCE8F0"
  ink: "#131212"
  ink-2: "#3B3A3A"
  muted: "#6E6E6E"
  rule: "#D9DCDF"
  disabled: "#DADADA"
  surface: "#F6F7F8"
  error: "#DB0000"
  error-bg: "#FDECEA"
  success: "#108043"
  success-bg: "#E8F3E8"

typography:
  font-head: "'Roboto Slab', ui-serif, Georgia, serif"
  font-body: "'Montserrat', ui-sans-serif, system-ui, sans-serif"
  font-mono: "'IBM Plex Mono', ui-monospace, Menlo, monospace"
  base-size: "14px"
  base-line-height: "1.5"
  h1: "22px / 700 / Roboto Slab"
  h2: "17px / 700 / Roboto Slab"
  h3: "14px / 600 / Roboto Slab"
  eyebrow: "11px / 700 / Montserrat / 0.08em tracked / uppercase"
  mono-weight: "300 (display) / 400 (inline)"

radii:
  sm: "4px"
  md: "8px"
  lg: "12px"
  xl: "20px"
  pill: "999px"

spacing:
  1: "4px"
  2: "8px"
  3: "12px"
  4: "16px"
  5: "24px"
  6: "32px"

shadows:
  1: "0 1px 2px rgba(32,59,86,.06), 0 2px 8px rgba(32,59,86,.06)"
  2: "0 4px 12px rgba(32,59,86,.08), 0 16px 32px rgba(32,59,86,.08)"

layout:
  max-width: "1280px"
  page-padding: "28px"
  page-padding-bottom: "64px"
---

# Design System

## Creative North Star

**Instrument panel.** This tool reads like precision equipment — the kind of display a clinician or air traffic controller would trust. Every element earns its place by carrying information, not by creating atmosphere. The reviewer should feel oriented the moment they land on any screen: hierarchy is unambiguous, the next action is obvious, and nothing competes with the data being reviewed.

Teal (`#008BB3`) is the active signal — it marks action, confidence, and the brand's presence. Navy (`#203B56`) is the authority anchor: it holds the structural weight of navigation, headers, and serious decision contexts. The off-white surface (`#F6F7F8`) keeps the field open, never sterile.

## Color System

Two primary roles, no blending.

**Teal (brand):** Active color. Buttons, focus rings, eyebrow labels, signal tags, active nav indicators. Reads as "this is actionable" or "this is the Bowst signal."

**Navy (secondary):** Authority anchor. Navigation background, card headers on confirmed/primary content, heading color across all pages. Reads as "this is structure."

**Tertiary (`#CCE8F0`):** Soft teal wash. Pending state badges, signal tag backgrounds, HHL data card headers. Low-saturation version of the brand — present but not loud.

**Semantic colors:** Success green for approved/high-confidence; error red for rejected/low-confidence; amber derivatives for medium/flagged. These are decision states, not decoration — they must remain distinguishable beyond color alone (weight, label text).

Surface is `#F6F7F8` — not white, not gray. Tinted slightly cool toward the brand. Cards sit on this surface in white (`#fff`), creating a 1-step elevation.

## Typography

Roboto Slab (serif, 700) for all headings: `h1` at 22px, `h2` at 17px, `h3` at 14px. Headings carry the navy color and tight line-height (1.25).

Montserrat for all body, UI labels, and buttons. 600 weight for buttons, filter chips, nav links, table headers. 400 for body copy and metadata. Eyebrow labels are Montserrat 700, 11px, 0.08em tracked, uppercase — used sparingly to label data sections.

IBM Plex Mono at weight 300 for NPI numbers, taxonomy codes, and other identifiers. Mono signals "this is a code/ID, not prose."

Body size is 14px across the application. Secondary metadata (dates, small annotations) at 12px. Table headers drop to 11px uppercase.

## Components & Patterns

**Record cards** (`bw-record`) are the primary review unit. They clearly delineate each provider as a discrete decision unit. Header bar uses the surface background with a bottom rule — this distinguishes the record identity (name, type, state) from the candidate content below. Candidate rows within a record are differentiated by confidence state (high/medium/other) through background tint and left accent.

**Buttons** have six variants with clear semantic hierarchy: `--primary` (teal fill, main CTA), `--secondary` (navy fill, secondary CTA), `--outline` (teal stroke, alternative action), `--ghost` (rule stroke, tertiary/neutral), `--danger` (error color, destructive), `--success` (green fill, confirm match). All buttons use 2px border so variant switches don't shift layout.

**Badges** (`bw-badge`) carry decision and confidence states. Always paired with text — never color alone. Pill-radius (`bw-r-sm`, 4px) keeps them compact but readable.

**Filter chips** (`bw-chip`) use the pill radius for scannable filter controls. Active state fills with brand teal, making the current filter immediately obvious without requiring a legend.

**Tables** (`bw-table`) use surface-tinted header rows, subtle row rules, and hover highlighting. Column headers are uppercase/tracked Montserrat 11px — clearly distinct from data rows without visual aggression.

## Motion & Interaction

Minimal and orienting. Button press uses `transform: translateY(1px)` on `:active` — tactile without being theatrical. Color transitions on hover at 0.12–0.15s linear or ease.

Nav link active states use a 3px bottom border that is set on page load, not animated in. The reviewer should never wait for UI to settle.

No animated entrances, no scroll-triggered reveals, no loading skeletons. If data is present, it is shown. If it is not, a clear empty state is shown.

## Layout & Spacing

Max content width 1280px, centered, with 28px page padding (64px bottom clearance for breathing room above the fold edge).

Filter bars and cards use `bw-r-xl` (20px) radius — generous, but not pill-like. Input and button elements use `bw-r-md` (8px) — functional, not soft.

Page rhythm comes from the card + filter-bar structure: filter bar sits above records, providing action context before the data. Page headers use flex `align-items: flex-end` so action buttons sit baseline-aligned with the heading — a precision detail.

Spacing scale is 4/8/12/16/24/32px. Component internals use 4–8px gaps. Section-level spacing uses 16–24px. Card padding is 24px on body, 16px on headers.
