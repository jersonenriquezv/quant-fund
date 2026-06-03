# Trade Log — Mobile App Plan

**Status:** Draft — awaiting review
**Author:** Jerson
**Date:** 2026-04-17
**Scope:** Mobilize the `/bybit` endpoint into a standalone mobile application while preserving the existing visual identity (black `#050505` + lime `#b2fd02` + Fraunces serif + JetBrains Mono + grain overlay).

> This plan covers brand, marketing, HLD, LLD, costs, and rollout. It assumes the parallel work on **confluence auto-snapshot / setup-quality grading** lands successfully (i.e. the backend will emit objective `setup_quality_score` and detected `confluences[]` at entry time, so the user stops manually grading). Where the plan hinges on that, it's called out explicitly.

---

## 1. Why a Mobile App (vs. PWA on Tailscale)

Current state — `/bybit` is desktop-ish served on Next.js, accessed on phone via Tailscale IP. Works but:

| Pain | Cause | App fix |
|---|---|---|
| Must open Tailscale, then browser, then URL | No home-screen icon / splash / instant launch | Native icon, cold-start < 1.5s |
| No native push notifications | Only Telegram today | APNs / FCM push for fill + close + weekly review |
| Poor mobile gesture UX (tap targets, swipe) | Desktop-first CSS | Native gestures, haptics, pull-to-refresh |
| Screenshot upload is a URL field | No camera integration | Capture → upload → attach in one flow |
| Biometric lock missing | Browser can't gate | Face ID / fingerprint gate on open |
| Offline resilience | Next.js page re-fetches every nav | Cached last-known state, sync on reconnect |
| Distributable as content artifact | "My Tailscale page" ≠ product | App Store / Play Store listing = brand asset |

**Verdict:** the journaling workflow is phone-first (user annotates after each fill, reviews in bed, reads weekly Claude review on Sunday). The app shape matches the use.

**Chosen stack:** **Expo (React Native) + existing FastAPI** — share the backend, rewrite the frontend, keep design tokens.

Why Expo:
- Single codebase iOS + Android, OTA updates via EAS
- User already owns the design system in CSS — can port to RN `StyleSheet` 1:1
- No Swift / Kotlin learning tax
- TypeScript end-to-end matches the web app
- Expo Router = file-based routing (same mental model as Next.js App Router)

Alternatives considered and rejected:
- **PWA-only (add manifest.json to existing Next.js):** zero native push on iOS until Safari parity catches up, no biometric, no App Store listing. Keep as fallback / tablet.
- **Swift + Kotlin native:** 2× build effort, zero reuse.
- **Flutter:** different language (Dart), no shared TS types with backend → out.

---

## 2. Brand

### 2.1 Name options (decide later)

| Name | Vibe | Risks |
|---|---|---|
| **Quant Fund** | On-brand with repo | Too corporate, boring |
| **Ledger** | Clean, journal-ish | Taken / crypto-wallet association |
| **Tradelog** | Literal | Generic |
| **Tape** | Ticker tape reference | Short, memorable, niche |
| **Hunt** | "Pattern-hunted. Graded." from current subtitle | Punchy, fits the serif-italic accent |
| **After** | Post-trade journaling framing | Ambiguous |

Recommendation: **Hunt** or **Tape**. Final call at review.

### 2.2 Positioning (one-liner)
> *"A post-trade journal that grades your setups before you do."*

Tagline under the mark: `trade · LOG/ · journal` (mirrors current `page.tsx` hero).

### 2.3 Visual system (carry over, don't redesign)
- Background `#050505`, text `#f5f5f7`
- Primary `#b2fd02` (lime), danger `#ff4d4d`, warn `#f59e0b`
- Fonts: **Fraunces** (display, serif italic), **JetBrains Mono** (data), **Instrument Serif** (tickers)
- Grain overlay at 3.5% opacity (reuse SVG turbulence)
- Border color `rgba(255,255,255,0.09)` for section dividers
- Typographic rhythm: 9–11px eyebrows with 0.2em letter-spacing, big serif numbers for PnL, mono for dates/prices

### 2.4 App icon
- Square: lime `/` slash on black (the `LOG/` motif)
- Adaptive icon (Android): foreground lime `/` on black layer
- Splash: black, grain, lime `/` pulsing once

### 2.5 Tone of voice
- Terse. No em-dashes of explanation. "Pattern-hunted. Graded." is the template.
- Italic-serif for subjective labels (*thesis*, *lesson*, *journal*)
- Mono caps for data (NET P&L, WIN RATE, CLOSED)

---

## 3. Marketing Strategy

This is where it intersects with `project_content_strategy.md` (jerdev_quant = "Intelligence Layer for Retail Traders"). The app is a **content asset**, not a product to sell.

### 3.1 Positioning vs. competitors
| Competitor | What they sell | Our counter-position |
|---|---|---|
| Edgewonk, TraderVue | Manual journaling SaaS, $20–40/mo | Free tier, post-trade auto-grading, bias toward crypto perps |
| 3Commas, Coinrule | Automation / copy-trading | We don't execute — we *review* |
| TradingView journal | Notes in charts | We grade setups, not track lines |
| Signal groups | Entries | We're the anti-signal — reflection tool |

**Moat:** the auto-snapshot of objective setup context (HTF bias, OI delta, CVD, funding, confluences detected by bot) = data competitors can't produce. Sibling session's work makes this possible.

### 3.2 Content pipeline → App funnel
Leverage existing **jerdev_quant** IG / X / TikTok:
- Each carousel ends with a subtle CTA — screenshot of the app's trade row with grade pill
- YT Shorts: "I graded 50 trades with this app" → walkthrough
- Carousel slide 5 (hard data) pulls from own app = proof of the "intelligence layer" claim

### 3.3 Launch phases

| Phase | Audience | KPI | Channels |
|---|---|---|---|
| **0 — Dogfood** (wk 1–4) | User only (TestFlight, Expo Dev Build) | Daily use, zero crashes, full annotation cycle works | none |
| **1 — Closed beta** (wk 5–12) | 10–30 retail crypto traders from jerdev_quant DMs | DAU/MAU > 40%, annotation rate > 60% of trades | DM waitlist, IG story gate |
| **2 — Public** (M4+) | Retail perps traders (crypto Twitter) | Installs, 7d retention | IG carousels, X threads, ProductHunt |
| **3 — Monetize** (M6+) | Phase 2 active users | Conversion > 5% on Premium | Newsletter + IG |

### 3.4 Monetization (DO NOT build yet — plan only)
- **Free:** manual annotation, 30 days of history, 1 account
- **Pro ($9/mo):** Claude weekly review, unlimited history, multi-account (Bybit + OKX + others), export CSV
- **Private cohort ($99/mo):** interpretation newsletter + private Telegram + app access. Reuses phase 3 of content strategy.

Payment: RevenueCat (no Stripe friction, handles both stores).

### 3.5 App Store Optimization
- Title: `Hunt — Trade Journal & Grader`
- Subtitle: `Post-trade reflection for perps traders`
- Keywords: `trading journal, crypto perps, bybit, trade log, setup grader, backtest, smc`
- Screenshots: lime accents on dark, same design language as IG carousels (consistency rule from content strategy)

---

## 4. High-Level Design (HLD)

### 4.1 System topology

```
  ┌──────────────────────────┐        ┌─────────────────────────────┐
  │   iOS / Android app      │        │   Nitro 5 (home server)     │
  │   Expo / React Native    │        │                             │
  │                          │        │   FastAPI  :8000            │
  │   ┌──────────────────┐   │  HTTPS │    /bybit/*  (existing)     │
  │   │ UI layer         │◄──┼────────┼──► /auth/*   (NEW)          │
  │   │ (mirrors web)    │   │  (TLS  │    /push/*   (NEW)          │
  │   └──────────────────┘   │  via   │    /mobile/* (NEW)          │
  │                          │ Cloud- │                             │
  │   ┌──────────────────┐   │ flare  │   PostgreSQL :5432          │
  │   │ Local cache      │   │ Tunnel)│    bybit_trade_annotations  │
  │   │ (React Query /   │   │        │    bybit_pending_orders     │
  │   │  AsyncStorage)   │   │        │    app_users (NEW)          │
  │   └──────────────────┘   │        │    app_push_tokens (NEW)    │
  │                          │        │                             │
  │   ┌──────────────────┐   │        │   Redis :6379               │
  │   │ Push receiver    │◄──┼─ APNs  │                             │
  │   │ (Expo Notif.)    │   │  FCM   │   bybit_watcher daemon      │
  │   └──────────────────┘   │        │   (emits push on fill)      │
  └──────────────────────────┘        └─────────────────────────────┘
                                              │
                                              ▼
                                      Bybit REST (read-only)
```

**Key decisions:**
- **No new backend service** — extend existing FastAPI. Keeps one process to operate.
- **Cloudflare Tunnel** in front of FastAPI (current Tailscale-only access isn't installable on App Store reviewer devices). Gives public HTTPS + DDoS protection, zero port-forward.
- **Auth:** JWT (short-lived access + refresh), stored in iOS Keychain / Android Keystore via `expo-secure-store`.
- **Push:** Expo Push Service → APNs/FCM. Backend stores device tokens in `app_push_tokens`, `bybit_watcher` fires on fill/close/invalidation.
- **No inbound writes to exchange from the app.** Bybit sync is read-only, same as today. App is journal-only. Removes a whole category of security risk.

### 4.2 Data flow (journal lifecycle)

```
User opens position on Bybit (manual, on phone)
  │
  ▼
bybit_watcher (60s poll) detects new fill
  │
  ├──► INSERT bybit_trade_annotations (status=open, context_snapshot=JSONB)
  │    └──► NEW: setup_quality_score + detected_confluences
  │         (from sibling session's snapshot work)
  │
  ├──► INSERT app_push_outbox (user_id, type=fill)
  │    └──► Expo push worker → phone notification
  │         "LONG BTC · fill @ 67,230 · quality 82/100 · 3 confluences"
  │
  └──► App receives push → opens /annotate/:id deep link
       │
       ▼
       User fills thesis, emotional_state, screenshot (camera)
       PATCH /bybit/annotations/:id
       Local cache invalidates → list re-fetches
```

On close, same path with `type=close`, PnL in the payload.

### 4.3 Offline strategy
- React Query with persist-to-AsyncStorage
- Reads: show last-cached list immediately, revalidate in background
- Writes: queue PATCH in outbox, flush on reconnect (mutation retry)
- Optimistic UI on annotation save (user types on subway → feedback instant)

### 4.4 Security
- JWT + refresh rotation on every access token refresh
- Backend enforces `user_id` ownership on every `/bybit/annotations` query (currently missing — SEE §8 gaps)
- TLS via Cloudflare, HSTS enforced
- Biometric gate on app open (opt-in, stored in Keychain flag)
- Rate limit `/auth/*` on FastAPI (10/min/IP)
- No exchange keys leave server — app never sees Bybit API credentials

---

## 5. Low-Level Design (LLD)

### 5.1 Screens (Expo Router file tree)

```
app/
  (auth)/
    login.tsx           — email + 6-digit code (passwordless)
    verify.tsx          — code input
  (tabs)/
    _layout.tsx         — bottom tabs: Log · Pending · Stats · Settings
    index.tsx           — Trade Log (current /bybit page ported)
    pending.tsx         — Pending orders (current inline section, expanded)
    stats.tsx           — Summary + equity chart + setup breakdown
    settings.tsx        — account, notifications, bio lock, logout
  annotate/
    [id].tsx            — form (current /annotate/[id] ported)
  pending/
    [id].tsx            — pending annotate form
  review/
    [week].tsx          — Claude weekly review reader
    index.tsx           — list of weekly reviews
```

### 5.2 Component inventory (port map)

| Web component | RN equivalent | Notes |
|---|---|---|
| `HeroStat` | `<HeroStat>` (View + styled text) | Keep animation via `react-native-reanimated` |
| `TradeRow` | `<TradeRow>` (Pressable) | Replace `<Link>` with `router.push` |
| `PendingRow` | `<PendingRow>` | Same |
| `EquityChart` (SVG) | `react-native-svg` (already in Expo) | Port verbatim — SVG API matches |
| `grain` overlay | `<Image>` with SVG data URI or skia | Optional on RN — could skip for perf |
| CSS `styled jsx` | `StyleSheet.create` | Mechanical translation |

### 5.3 Typography
- Load fonts via `expo-font` on app boot:
  - `Fraunces-Regular`, `Fraunces-Italic`, `Fraunces-Medium`
  - `JetBrainsMono-Regular`, `JetBrainsMono-Bold`
  - `InstrumentSerif-Regular`
- Splash screen holds until `useFonts` ready (no FOUT)

### 5.4 New backend endpoints

```python
# dashboard/api/routes/auth.py (new)
POST /auth/request-code        { email }          → { request_id }
POST /auth/verify-code         { request_id, code } → { access, refresh }
POST /auth/refresh             { refresh }         → { access, refresh }
POST /auth/logout              (auth)              → 204

# dashboard/api/routes/push.py (new)
POST /push/register            { expo_token, device_id } (auth)
POST /push/unregister          { device_id }             (auth)

# dashboard/api/routes/mobile.py (new — convenience rollups)
GET  /mobile/home              → summary + last_10_trades + pending_count (auth)
GET  /mobile/reviews           → list weekly reviews (auth)
GET  /mobile/reviews/:week     → markdown body (auth)
```

Existing `/bybit/*` endpoints get `Depends(current_user)` added — SEE §8.

### 5.5 New DB tables

```sql
CREATE TABLE app_users (
  id          BIGSERIAL PRIMARY KEY,
  email       TEXT UNIQUE NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  last_login  TIMESTAMPTZ
);

CREATE TABLE app_login_codes (
  request_id  UUID PRIMARY KEY,
  email       TEXT NOT NULL,
  code_hash   TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ
);

CREATE TABLE app_push_tokens (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT REFERENCES app_users(id) ON DELETE CASCADE,
  expo_token  TEXT NOT NULL,
  device_id   TEXT NOT NULL,
  platform    TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, device_id)
);

-- Multi-user prep (not multi-user yet, but add column)
ALTER TABLE bybit_trade_annotations ADD COLUMN user_id BIGINT DEFAULT 1;
ALTER TABLE bybit_pending_orders    ADD COLUMN user_id BIGINT DEFAULT 1;
-- Backfill user_id=1 (= Jerson) on all existing rows.
```

### 5.6 Push payload shape

```json
{
  "to": "ExponentPushToken[...]",
  "title": "LONG BTC · filled",
  "body": "67,230.5 · quality 82/100 · 3 confluences",
  "data": {
    "type": "fill",
    "annotation_id": 1234,
    "deep_link": "hunt://annotate/1234"
  },
  "sound": "default",
  "priority": "high"
}
```

Deep links registered via `expo-linking`, `scheme: "hunt"`.

### 5.7 State management
- **Server state:** `@tanstack/react-query` (v5), persist via `@tanstack/react-query-persist-client` + `AsyncStorage`
- **Client state:** Zustand for UI-only (filters, which chip active)
- **Forms:** React Hook Form + Zod (share schema with backend Pydantic via codegen if time permits)

### 5.8 Build + release
- EAS Build (managed workflow, cloud builds)
- EAS Update for JS-only OTA patches
- Two profiles: `preview` (internal TestFlight / Play Internal), `production` (store)
- Semantic versioning on `app.json`

---

## 6. Integration with parallel snapshot/auto-grade work

The sibling session is adding:
- Backend writes `setup_quality_score` (0–100) + detected `confluences[]` into `bybit_trade_annotations` at entry time
- Replaces manual confluence selection + grade-self

This plan adapts as follows:
- `/annotate/:id` screen: confluences become **read-only display** (lime chips, ordered by strength). User only edits thesis + emotional_state + lesson + screenshot.
- Trade Log row: `quality_score` replaces manual `★★★★☆` stars (same slot, lime gradient based on score).
- Push body references `quality` not `stars`.
- Grade self (A/B/C/D/F) becomes optional — kept as "how I felt about the decision" distinct from "how objectively strong the setup was".

**Dependency:** app work MUST land after snapshot backend stabilizes, else UI has two sources of truth.

---

## 7. Rollout Plan — Phased

| Phase | Duration | Deliverables | Gate |
|---|---|---|---|
| **P0 — Foundations** | Week 1 | Cloudflare Tunnel on FastAPI, auth endpoints, JWT, `app_users` + `app_push_tokens` tables, user_id backfill, ownership checks on `/bybit/*` | Postman flow green, existing web still works |
| **P1 — Expo scaffold** | Week 2 | Expo project, Expo Router tabs, fonts loaded, design tokens, login + verify screens | Log in on device, see empty Log screen |
| **P2 — Core read path** | Week 3 | Log list, stats, pending list, equity chart ported | Parity with web `/bybit` at 375px |
| **P3 — Annotation write path** | Week 4 | Annotate form, pending form, camera screenshot upload (presigned URL → S3 or local disk), offline queue | Full journal cycle works on phone |
| **P4 — Push** | Week 5 | `bybit_watcher` emits to outbox, push worker, deep links | Fill Bybit → phone notification in < 90s |
| **P5 — Weekly review reader** | Week 6 | Reviews index + markdown viewer | Open Sunday review on phone in bed |
| **P6 — Polish + TestFlight** | Week 7 | Biometric gate, haptics, pull-to-refresh, skeleton states, error boundaries | Dogfood stable |
| **P7 — Closed beta** | Week 8–12 | DM 10–30 waitlist users, collect feedback, iterate | Crash-free > 99.5% |
| **P8 — Public launch** | M4 | Store submissions, IG + X campaign, ProductHunt | Public listing live |
| **P9 — Monetization** | M6 | RevenueCat, Pro tier, Claude weekly review paywall | First paid user |

---

## 8. Known Gaps / Risks / Decisions Pending

### Gaps in current system that block the app
1. **No authentication on `/bybit/*`** — today it's Tailscale-gated. Public tunnel needs JWT. Must add `Depends(current_user)` to every endpoint.
2. **No `user_id` column on annotation tables** — added in P0 migration above.
3. **No push outbox** — `bybit_watcher` currently emits Telegram only. Needs a second sink.
4. **No screenshot storage** — URLs are manual today. Need S3-compatible (Cloudflare R2 = cheap) or local-disk + reverse proxy.
5. **Weekly reviews are markdown files in `docs/bybit_reviews/`** — need a simple endpoint to list + read them.

### Risks
- **App Store review:** crypto/trading apps get extra scrutiny. Mitigation: position firmly as journal, not broker. No order execution. No signal sharing.
- **Cloudflare Tunnel outage** = app can't talk to backend. Mitigation: offline-first cache, retry logic, optional fallback to Tailscale for dogfood.
- **OKX / Bybit API geo issues:** irrelevant — API keys live on server, app never calls exchanges.
- **Single-user assumption everywhere:** entire codebase assumes "Jerson is the user". Multi-user will require a pass — scoped to P9, not P0.
- **Parallel snapshot work slippage:** if sibling session not done by P3, fall back to manual confluences + mark the feature as "V2".

### Decisions pending (need user call)
- [ ] **App name** (Hunt / Tape / other)
- [ ] **Hosting for public tunnel** — Cloudflare Tunnel (free, recommended) vs. VPS (relates to the "migrate off Nitro 5 when capital > $1k" note in MEMORY)
- [ ] **Screenshot storage** — Cloudflare R2 ($0.015/GB, free 10GB) vs. local disk behind FastAPI
- [ ] **Passwordless email code vs. Apple/Google OAuth** — OAuth = smoother UX, +1 day work
- [ ] **Whether to also ship a PWA from the existing Next.js** as phase-0 quick win — one-hour work, no store friction
- [ ] **Multi-account at launch or later?** (adding OKX + other Bybit subs). Recommendation: later.
- [ ] **Tablet-optimized layout** — iPad / Android tablet — treat as bonus in P8

---

## 9. Cost Breakdown

### 9.1 One-time
| Item | Cost | Notes |
|---|---|---|
| Apple Developer Program | $99 / year | required for TestFlight + App Store |
| Google Play Console | $25 one-time | required for Play Store |
| Domain (e.g. `hunt.app` or `jerdev.quant`) | $10–40 / year | brand asset |
| Design assets (icon, splash, store screenshots) | $0 (self) or $200 (Fiverr) | |
| **Total** | **~$135 + $200 opt** | |

### 9.2 Recurring (per month)
| Service | Tier | Cost |
|---|---|---|
| **EAS Build** | Free (30 builds/mo) or Production ($99/mo) | $0 initially, $99 at scale |
| **Expo Push** | Free (unlimited) | $0 |
| **Cloudflare Tunnel** | Free | $0 |
| **Cloudflare R2** (screenshots) | 10GB free, then $0.015/GB | ~$0 for dogfood, ~$2/mo at beta |
| **Sentry** (error monitoring) | Developer free (5k events/mo) | $0 |
| **RevenueCat** (when monetizing) | Free up to $10k MTR, then 1% | $0 until revenue |
| **Claude API** (weekly reviews — already paid) | existing | — |
| **Postmark / Resend** (login code emails) | Free tier (100/day) | $0, $10/mo at scale |
| **Hosting** | Nitro 5 (already paid) | $0 |
| **Total dogfood→beta** | | **~$2–12 / mo** |
| **Total at 1k users** | | **~$150 / mo** (EAS + R2 + email + Sentry) |

### 9.3 Revenue model (for context, not to build)
- 5% conversion × $9/mo × 1k users = $450 MRR
- Break-even at ~30 paid users

---

## 10. Open questions for review

1. Is **Hunt** the name, or go with something else? Do you want a wordmark designed?
2. Do we ship the **Next.js PWA as phase 0** (add `manifest.json` + service worker, one day of work) to get some value on iOS home screen before the native app lands?
3. **Cloudflare Tunnel on the Nitro 5** — OK to expose FastAPI publicly (with auth)? Or keep Tailscale-only and skip store launch?
4. **Screenshot storage:** R2 or local-disk-via-FastAPI? R2 costs ~pennies but adds a vendor.
5. **Should the app include read-only access to the OKX bot's `/` dashboard** too (shadow monitor, live setups), or strictly the Bybit journal? (Recommend: strictly journal for V1 — OKX bot dashboard is monitoring, different UX.)
   - **RESOLVED 2026-06-03:** Journal stays V1. The **Chart** (`/chart` — klinecharts overlay + analysis) gets added as a **V2 module via WebView** (klinecharts has no native RN port). Analysis = OB/FVG overlay + `/topdown` brief + new on-demand AI analysis. Exposed behind JWT on the same tunnel. Full plan: `docs/plans/mobile-chart-module-2026-06-03.md`.
6. **Confluence auto-snapshot work:** what's the ETA from the parallel session? Plan assumes it lands before P3 (week 4). If slipping, we fall back to V1 manual confluences + V2 auto.
7. **Do you want weekly Claude review push** to fire automatically every Sunday 10am local (`scripts/weekly_review_bybit.py` + push), or user-initiated only?

---

## 11. Next steps (after review)

1. User reviews + annotates this doc (inline `<!-- comment -->` or new `## Review notes` section)
2. Lock phase ordering and name
3. Spike P0 (Cloudflare Tunnel + auth) — 2–3 days, reversible
4. Create Expo repo at `dashboard/app/` (or separate repo — TBD)
5. Port `/bybit` page to RN as P2 proof-of-design

---

*Keep this doc as source-of-truth for app decisions. Update on every material change, same rule as `SYSTEM_BASELINE.md`.*
