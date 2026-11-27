---
name: Crypto Portfolio
description: A self-hosted, read-only net-worth ledger for one person, read on a phone in the dark.
colors:
  signal-green: "#22E58B"
  signal-red: "#FF5C73"
  caution-amber: "#F5B84B"
  focus-blue: "#8AB4FF"
  night: "#0B0E14"
  surface: "#161B22"
  surface-raised: "#1C222B"
  surface-pressed: "#232A34"
  hairline: "#252C36"
  hairline-strong: "#323B47"
  ink: "#E9EDF2"
  ink-muted: "#A3ADBA"
  ink-faint: "#8590A0"
typography:
  display:
    fontFamily: "Manrope, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "clamp(22px, 10.5vw, 46px)"
    fontWeight: 800
    lineHeight: 1.05
    letterSpacing: "-0.035em"
    fontFeature: "tnum"
  headline:
    fontFamily: "Manrope, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "28px"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "-0.03em"
    fontFeature: "tnum"
  title:
    fontFamily: "Manrope, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "16px"
    fontWeight: 800
    lineHeight: 1.45
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Manrope, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 500
    lineHeight: 1.45
  label:
    fontFamily: "Manrope, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.45
rounded:
  badge: "6px"
  inner: "10px"
  control-sm: "12px"
  control: "14px"
  panel: "16px"
  card: "22px"
  sheet: "26px"
  pill: "999px"
spacing:
  tile-gap: "8px"
  row-gap: "12px"
  gutter: "16px"
  card-pad: "20px"
components:
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.card}"
    padding: "{spacing.card-pad}"
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.night}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "44px"
  button-primary-disabled:
    backgroundColor: "{colors.surface-pressed}"
    textColor: "{colors.ink-faint}"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "44px"
  button-ghost:
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "44px"
  field:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    height: "48px"
  filter-chip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.pill}"
    padding: "0 12px"
    height: "36px"
  filter-chip-selected:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.night}"
  status-pill:
    backgroundColor: "{colors.surface-pressed}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.pill}"
    padding: "0 10px"
    height: "26px"
  metric-tile:
    backgroundColor: "{colors.surface-raised}"
    rounded: "{rounded.control}"
    padding: "10px"
---

# Design System: Crypto Portfolio

> Extracted on 2026-11-27 from the running PWA (`static/style.css`, `static/index.html`). The
> North Star name and the named rules are a proposal written without an interview; the tokens
> and components are what the code does today.

## Overview

**Creative North Star: "The Night Ledger"**

A ledger opened on a phone, several times a day, usually in a dim room. The screen is nearly
black and the numbers are the brightest thing on it. Nothing glows for decoration: the only
saturated colour on a healthy screen is the sign of a money move. Surfaces are told apart by a
half-step of tone and a hairline, never by shadow.

The system is dense but calm. One column, 560px at most, built from rounded cards stacked with
even gaps. Each card answers one question and states its own caveats (how many observations,
which source failed, which figure is an estimate) next to the number it qualifies. Type is one
family, Manrope, pushed to its heavy weights for figures and titles and kept at semibold for
everything that explains them.

**Key Characteristics:**
- Near-black canvas, tonal surfaces, hairline borders, no resting shadows.
- One typeface; weight and size carry hierarchy; all figures are tabular.
- Green and red mean "money went up / down" and nothing else.
- Every caveat sits beside its number; nothing fails silently.
- Mobile first: 44px touch targets, one thumb-reachable tab bar, bottom sheets.
- Interface copy is English.

## Colors

A monochrome blue-grey scale carries the whole interface; four signal colours are spent only
on meaning.

### Primary
- **Signal Green** (`signal-green`): a gain, a deposit, a passing check, the "on" state of a
  switch, the text caret. At 12% opacity it is the background of the chip that carries the
  same meaning.
- **Signal Red** (`signal-red`): a loss, a withdrawal or fee, a failed source, an invalid
  field, destructive actions. Same 12-13% tint for its chip.

### Secondary
- **Caution Amber** (`caution-amber`): something needs a look, not something is broken:
  partial data, items awaiting review, an over-weight allocation. Also the icon tint of Earn
  rewards in the timeline.

### Tertiary
- **Focus Blue** (`focus-blue`): the keyboard focus ring, links, informational notices,
  internal transfers, an under-weight allocation. Never a call-to-action fill.

### Neutral
- **Night** (`night`): the page and the bottom sheet.
- **Surface** (`surface`): every card.
- **Surface Raised** (`surface-raised`): things that sit inside a card: tiles, fields,
  nested panels, hover and press on rows.
- **Surface Pressed** (`surface-pressed`): the selected segment, the active pill, the pressed
  state, neutral badges.
- **Hairline** (`hairline`) and **Hairline Strong** (`hairline-strong`): card borders and row
  dividers; the stronger one outlines controls and floating layers.
- **Ink** (`ink`): figures, titles, primary text, and the fill of the primary button.
- **Ink Muted** (`ink-muted`): secondary text and unselected controls.
- **Ink Faint** (`ink-faint`): captions, units, the cents of the hero figure. It is the
  lightest text allowed: nothing dimmer passes AA on Surface.

### Named Rules
**The Colour Is Data Rule.** Green and red appear only on a signed amount or a pass/fail
state. A screen with no move and no failure is entirely grey.

**The Never Colour Alone Rule.** A gain or loss always carries a sign and an arrow as well as
its colour; a status always carries a word.

**The Tint Rule.** A signal colour used as a background is the same hue at 7-14% opacity over
the surface, with the full-strength hue as text. Solid signal fills do not exist.

## Typography

**Display Font:** Manrope variable, weights 200-800 (with ui-sans-serif, system-ui)
**Body Font:** Manrope
**Label/Mono Font:** the system monospace, for addresses and keys only, at 16px

**Character:** geometric and compact, with open numerals that stay legible at heavy weights.
Hierarchy comes from weight (800 for figures and titles, 700 for controls, 600 for
explanation), not from a second family.

### Hierarchy
- **Display** (800, 22-46px sized from the character count, 1.05, -0.035em): the net-worth
  figure only. Its cents drop to 700 in Ink Faint.
- **Headline** (800, 28px, 1.1, -0.03em): the one lead figure of a card, such as the period
  delta in "Why it moved".
- **Title** (800, 16px, -0.015em): card titles. Sheet titles step up to 19px.
- **Body** (500-600, 15px, 1.45): running text; 13-14px inside cards and rows.
- **Label** (600-800, 12-13px): captions, column heads, tile labels, chips. Day headers in
  the timeline are 12px, 800, uppercase, +0.04em. The floor is 11px, used only for badges and
  tab-bar labels.

### Named Rules
**The Tabular Rule.** Every number that can change is set with tabular figures, so columns
line up and a refreshing value does not jitter.

**The Money Is Never Cut Rule.** A balance is never ellipsised. The column widens, the figure
shrinks, or the unit drops to a second line.

## Layout

One column, centred, 560px at most, with a 16px page gutter. Cards stack with 16px between
them; card content is inset 20px, list rows 16px. Inside a card, tiles sit in a 2- or 3-column
grid with 8px gaps. Below 360px, secondary columns (the share bar, the order amount) fold away
or wrap rather than squeeze.

A sticky translucent top bar (56px) holds the section title, refresh and settings. A fixed
translucent tab bar (56px plus the safe area) holds the three sections. Editing happens in a
bottom sheet that becomes a centred dialog from 640px up. Toasts float just above the tab bar.

### Screen map

| Tab | Its question | What lives there today |
|---|---|---|
| **Portfolio** | What is it worth right now? | Hero (total, 24h chip, chart, range pills), status line (origin, age), failed-sources notice, source filter, asset list with expandable rows |
| **Analytics** | Why, and how well? | One period selector pinned under the title, then cards: Why it moved, Performance, Risk, Allocation & rebalancing |
| **Activity** | What happened to the money? | Period select, source and kind filter chips, review banner, one card per day of flows |

### Named Rules
**The One-Second Rule.** The Portfolio tab must stay readable in one second: total, 24h move,
chart. It receives no new cards. New facts about the present either join the status line under
the hero (and hide themselves when healthy) or go inside an asset row.

**The One Question Rule.** A card answers one question, named in its title. A new feature
extends the card that already owns its question before it earns a card of its own.

**The Quiet When Healthy Rule.** A warning element takes no space while there is nothing to
warn about. No "all good" chips.

### Phase 3 placement (N1-N9)

Where each roadmap idea lands. Detailed design happens per wave, on real data; this map only
fixes location so nine features do not become nine cards. Net result: **one** new card.

| ID | Idea | Lands in | Form |
|---|---|---|---|
| N1 | Earn yield | Analytics, new card "Earn income" between Risk and Allocation | Table per asset (listed APR, realised 7d / 30d, per day) and one line of lifetime income by year. Like Allocation, it ignores the period selector and says so. |
| N2 | Inferred trades | Activity timeline | A new kind with a filter chip; neutral icon; an "inferred" tag on every such row; one "Unexplained" row per day when a residue remains. |
| N2 | Data health | Portfolio status line (existing) | The age text turns amber past 130 min; the existing failed-sources notice stays the place for source errors; gap count joins the line only when above zero. No new badge. |
| N3 | Peg check | Portfolio status line | One amber status pill ("USDC -0.4%" or "peg unchecked"), absent when on peg. |
| N4 | Cost basis | Portfolio asset row, expanded detail | Average entry and unrealised PnL as two more lines of the existing detail list; "no cost basis" stated in words where unknown. Real trades replace the inferred rows in Activity. |
| N5 | Analyst kit | none | CLI and SQL only. |
| N6 | Disbursement plan and stress | Analytics: Allocation card (plan), Risk card (stress) | Plan progress and the next order reuse the suggested-orders rows; the stress line and its slider join Risk. |
| N7 | EUR lens | Hero label row; "Why it moved" buckets | A two-option switch (USD / EUR) beside the privacy eye; the FX effect is one more bucket tile. |
| N8 | NAV vs deposited capital | Hero chart | A second, dashed, Ink Faint line with a one-line legend; hidden where flow history is missing. |
| N9 | Investment notes | Activity day block; "Why it moved" | A note is a row of its day; the attribution card quotes the notes of its period. |

Open at this level: the status line can now hold four things on a bad day (origin, age, gaps,
peg). It wraps today; if it wraps to three lines on a 360px screen, peg and gaps merge into
one "2 warnings" pill that opens the detail.

## Elevation & Depth

Flat by tone. Depth is three steps of surface (Night, Surface, Surface Raised) separated by
1px hairlines. Shadows are reserved for things that float above the page; blur is reserved
for the two bars and the sheet backdrop.

### Shadow Vocabulary
- **Sheet lift** (`0 -12px 40px rgba(0,0,0,0.5)`): the bottom sheet.
- **Toast lift** (`0 10px 30px rgba(0,0,0,0.5)`): toasts.
- **Tip lift** (`0 6px 18px rgba(0,0,0,0.45)`): the chart scrub tooltip.
- **Knob** (`0 2px 6px rgba(0,0,0,0.4)`): the switch thumb.

### Named Rules
**The Flat At Rest Rule.** Nothing that scrolls with the page casts a shadow. A card that
needs emphasis gets a tinted wash at its top edge (the hero) or a tinted border (a notice).

## Shapes

Generously rounded, in a strict nesting order: the outer shape is always rounder than what it
contains. Sheet 26px, card 22px, panels inside a card (notices, nested lists, the segmented
control) 16px, controls 14px, small controls 12px, the highlight inside a pill 10px, badges
6-7px. Chips, status pills and coin avatars are fully round. Borders are always 1px. Icons are
one stroke set: 1.85px, round caps and joins, 20px (16px and 14px variants).

## Components

### Buttons
- **Shape:** 14px corners, 44px minimum height, 700 weight, 14px text. The full-width form
  action is 52px tall with 16px corners.
- **Primary:** Ink fill with Night text. There is no coloured primary: the brightest neutral is
  the call to action. Disabled becomes Surface Pressed with Ink Faint text, at full opacity.
- **Secondary:** Surface Raised with a Hairline Strong outline.
- **Ghost / Danger:** text only (Ink Muted, or Signal Red); danger gains a red tint when armed.
- **Press:** scales to 0.98 over 120ms. Hover exists only on devices that hover.

### Chips
- **Filter chip:** round, 36px tall (44px hit area), Surface with a strong outline, carrying a
  count bubble. Selected inverts to Ink fill. A filter with no results dims to 45%; it is never
  removed.
- **Move chip:** round, tinted green, red or neutral, with arrow, amount and percent. On the
  hero it is a button that opens the attribution peek and shows a chevron.
- **Status pill:** round, 26px, a 7px dot plus a word; neutral, ok, warn or error tint. The dot
  pulses while busy.
- **Badges:** 19-22px tall, 6-7px corners, 11px 800 text: source, kind, "review".

### Cards / Containers
- **Corner Style:** 22px.
- **Background:** Surface, 1px Hairline border, no shadow.
- **Header:** title (16px 800) and one line of sub-text (12px, Ink Faint) that states the scope
  or the sample size, inset 20px.
- **Hero variant:** a 180px radial wash of green or red at 10% in the top-left corner, set by
  the direction of the day.
- **Metric tile:** Surface Raised, 14px corners, 10px padding: label, 17px 800 figure, note. A
  tile without enough data goes grey at 55% opacity and keeps its explanation.

### Inputs / Fields
- **Style:** Surface Raised, Hairline Strong outline, 14px corners, 48px tall, 16px text (so
  iOS does not zoom).
- **Focus:** Focus Blue border plus a 3px blue halo at 16%.
- **States:** invalid turns the border red, valid green; one line of feedback below with an
  icon carrying the state.
- **Switch:** 50x30 track, green-tinted when on, thumb stretches while pressed.

### Navigation
- **Tab bar:** three equal tabs, icon in a 52x28 round highlight plus an 11px label; selected
  is Ink on a Surface Pressed highlight. A red count bubble marks items awaiting review.
- **Segmented control:** 2 or 3 equal segments in a Surface tray with 16px corners; a segment
  may carry a figure under its label.
- **Pills:** timeframe and period selectors; text-only, the selected one on a Surface Pressed
  highlight; 44px hit area around a smaller drawn pill.

### Lists
- **Asset row:** 66px, coin avatar (38px, logo or hue-tinted initials), name and quantity,
  share with a 3px bar, value right-aligned. Expands in place to a three-column detail list.
- **Timeline row:** 62px, a 36px round icon tinted by kind (green in, red out, blue transfer,
  amber reward, neutral otherwise), text, signed amount. Rows needing review carry an amber
  wash from the left.
- **Dividers:** 1px Hairline between rows; the list itself is a card.

### Notices
A 16px-cornered panel tinted amber (default), blue (info) or red (error) at 7-8%, with a
matching 1px border, an icon and pale text. It sits directly under the thing it qualifies.

### Privacy blur (signature)
Every money amount is wrapped so that the privacy toggle blurs it (10px for totals, 6px for
secondary amounts) and makes it unselectable. Percentages stay readable. A new figure in
dollars that is not wrapped is a bug.

### Bottom sheet
Night background, 26px top corners, a grip, a scrolling body and a pinned footer for the
primary action. Enters by sliding up over 420ms on the house ease-out
(`cubic-bezier(0.16, 1, 0.3, 1)`); state changes elsewhere take 200ms. Reduced-motion turns
all of it off.

## Do's and Don'ts

### Do:
- **Do** wrap every dollar amount in the privacy blur and set it in tabular figures.
- **Do** pair every gain or loss colour with a sign and an arrow.
- **Do** state the caveat where the number is: sample size in the card sub-line, "inferred" or
  "estimate" as a tag on the row, missing data in words instead of a zero.
- **Do** keep touch targets at 44px, extending the hit area invisibly when the drawn control
  is smaller.
- **Do** extend an existing card, the status line or an asset row before adding a card.
- **Do** give every card a loading skeleton, an empty state that says what is missing, and an
  error state that leaves the rest of the tab usable.

### Don't:
- **Don't** use green or red for anything but a signed amount or a pass/fail state.
- **Don't** add resting shadows, gradients as decoration, or a second typeface.
- **Don't** add a card to the Portfolio tab.
- **Don't** show a healthy-state chip; warnings appear only when there is something to say.
- **Don't** ellipsise money, or annualise a return over a period shorter than a year.
- **Don't** go below 11px text or Ink Faint for contrast.
