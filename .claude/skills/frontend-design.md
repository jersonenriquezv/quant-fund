This skill guides creation of distinctive, production-grade frontend interfaces for a real-time crypto trading dashboard. The dashboard monitors a quant trading bot 24/7 — clarity, speed, and data density are paramount.

The user provides frontend requirements: a component, page, or interface to build or improve. They may include context about the purpose, audience, or technical constraints.

## Design Thinking

Before coding, understand the context:
- **Purpose:** What data does this show? What decision does it support?
- **Priority:** Is this glanceable (status dot) or deep-dive (trade log)?
- **Density:** Trading UIs reward information density — maximize signal per pixel.
- **Urgency:** Some data is time-critical (open positions, PnL) vs reference (whale log, news).

## Design System — Trading Dashboard

### Aesthetic Direction: Dark Terminal Elegance
A Bloomberg Terminal meets modern glassmorphism. Dense, data-rich, but refined. Not generic fintech — this is a personal command center for a quant trader.

### Color Palette (already defined in CSS variables)
- **Background:** Pure black (#000) → near-black (#0a0a0a). No grays — use opacity layers.
- **Cards:** White at 4% opacity with backdrop blur. Depth through transparency, not shadows.
- **Profit/Long:** Emerald green (#10b981). Never lime or neon.
- **Loss/Short:** Clean red (#ef4444). Never pink or orange-red.
- **Accent:** Blue (#3b82f6) for neutral interactive elements.
- **Warning:** Amber (#f59e0b) for alerts and caution states.
- **Text hierarchy:** Primary (#f5f5f7) → Secondary (60% white) → Muted (35% white). Three levels, no more.
- **Borders:** White at 8% opacity. Subtle separation, never harsh lines.

When adding new elements, derive colors from these variables. Never introduce new hues — use opacity variations of existing colors.

### Typography
- **Data/Numbers:** JetBrains Mono (primary). All numerical values must use `font-variant-numeric: tabular-nums` for column alignment.
- **Body text:** Same monospace stack (JetBrains Mono → Fira Code → Cascadia Code). Consistency over variety.
- **Size scale:** 10px (micro labels) → 11px (table headers, badges) → 12px (table data, body) → 13-14px (card content) → 16-22px (hero numbers like price, PnL).
- **Weight:** 400 normal, 500 medium (headers), 600 semibold (badges), 700 bold (hero numbers only).
- **Letter spacing:** 0.05-0.1em on uppercase labels. Never on body text.
- **DO NOT** use display fonts, serif fonts, or decorative typefaces. This is a data interface, not a landing page.

### Layout Principles
- **Grid-based:** CSS Grid with explicit column/row placement. The dashboard uses a 3-column layout (1fr 1fr 340px) with full-width spans for secondary data.
- **Density over whitespace:** 8px gaps, 16px card padding. Trading dashboards are not marketing pages — compact is correct.
- **Visual hierarchy through size:** Hero numbers (price, PnL) are 2-3x larger than surrounding data. No need for decorative elements — the data IS the design.
- **Card-based composition:** Every data group gets a `.card` with the established glass effect. No bare data floating outside cards.

### Component Patterns

**Status Indicators:**
- Colored dots (8px) with matching glow (`box-shadow`) for system health
- Pill badges (border-radius: 100px) for categorical data (LONG/SHORT, LIVE/DEMO)
- Color-coded left borders (3px) for win/loss on trade cards

**Data Tables:**
- Uppercase headers (11px, muted, letter-spaced)
- Row hover with subtle background shift
- Right-align all numbers (`.num` class)
- Hide low-priority columns on mobile via dedicated CSS classes
- Horizontal scroll wrapper (`.scroll-y`) for overflow

**Charts:**
- Dark backgrounds — no white chart areas
- Use the profit/loss color scheme for positive/negative values
- Minimal gridlines (white at 5-8% opacity)
- Interactive tooltips over legends when space is limited

**Real-time Data:**
- `slideIn` animation (0.3s) for new entries
- `pulse` animation for status changes
- `shimmer` skeleton loading for async data
- Never block the UI waiting for data — show skeletons immediately

**Buttons & Interactive:**
- Translucent backgrounds matching their semantic color (red for destructive, green for confirm)
- 1px border with matching color at higher opacity
- `cursor: pointer` on all clickable elements
- Hover: increase background opacity. Transition: 0.2s ease.
- Disabled: 50% opacity, `cursor: wait` or `not-allowed`

### Micro-interactions & Motion
- **Page load:** No elaborate entrance animations. Data should appear fast.
- **New data:** `slideIn` (translateY -8px → 0, 0.3s) for new rows/cards.
- **State changes:** Color transitions (0.2-0.3s) for PnL updates, status changes.
- **Hover:** Subtle background brightening. No scale transforms on data elements.
- **Scrollbars:** Custom thin (4px) with transparent track, 10% white thumb.
- **DO NOT** add: page transitions, parallax, scroll animations, decorative particles, or any motion that delays access to data.

### Accessibility
- Minimum contrast 4.5:1 for text (already met with current palette)
- Never use color alone to convey meaning — pair with icons, text, or position
- Keyboard focus states: visible outline using `--accent` color
- `prefers-reduced-motion`: disable pulse/shimmer animations
- Touch targets minimum 44px on mobile

### Anti-patterns — NEVER do these:
- White/light backgrounds or light mode (this is a dark terminal)
- Generic AI aesthetics (purple gradients, Inter font, rounded-everything)
- Marketing-style hero sections, testimonials, or CTAs
- Decorative illustrations or stock imagery
- Excessive border-radius (max 12px on cards, 100px on pills only)
- Drop shadows (use opacity layers and backdrop-blur instead)
- Loading spinners (use skeleton shimmer)
- Modal dialogs for non-critical actions (use inline expansion)
- Tooltips that require hover on mobile (use tap-to-expand)

## Mobile Responsiveness (MANDATORY)

The dashboard has 2 breakpoints in `globals.css`:
- **Tablet (max-width: 1023px):** 2-column grid, sidebar items go full-width
- **Mobile (max-width: 639px):** Single column, reduced padding (12px cards, 4px outer)

Rules for every component:
- Test at 375px width (iPhone SE) — nothing should overflow
- Use CSS classes (not inline styles) for responsive layout changes
- Hide low-priority table columns on mobile (`display: none` classes)
- Tables must scroll horizontally on narrow screens
- Position grids collapse: 6-col → 3-col on mobile
- Hero numbers scale down (22px on mobile vs 28px on desktop)
- Buttons go full-width on mobile when they're action-critical

## Tech Stack
- **Framework:** Next.js (App Router, `"use client"` components)
- **Styling:** Vanilla CSS with CSS variables (no Tailwind, no CSS-in-JS)
- **Data:** Custom `useWebSocket` hook for real-time data
- **Charts:** Lightweight canvas or SVG (no heavy charting libraries unless justified)
- **State:** React hooks (useState, useEffect, useCallback). No state management library.

When building new components, follow the existing patterns in `dashboard/web/src/components/`. Every component exports a named function, receives data via props or hooks, and uses the established CSS class conventions.
