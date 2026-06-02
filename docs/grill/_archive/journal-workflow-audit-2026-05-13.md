# Journal workflow audit — Phase 1 (bybit-journal-enforcement)
**Date:** 2026-05-13
**Plan:** docs/plans/_archive/bybit-journal-enforcement.md
**Tracer goal:** identify the actual failure point in the current annotation pipeline.

## Stage-by-stage results

| Stage | What it does | Status | Evidence |
|-------|--------------|--------|----------|
| 1. Watcher running | `bybit_watcher.py` polls Bybit every 60s | ✅ HEALTHY | Container `quant-fund-bybit-watcher-1` up 13 days, `health=healthy`. Logs show continuous activity through 2026-05-13. |
| 2. Annotation insert | On position open, inserts row in `bybit_trade_annotations` | ✅ WORKING | 37 rows in DB matching 37 closed_pnl trades. Logs show `OPEN ... annot_id=N` consistently. Auto-classification fills `auto_setup_type` (B_sweep/D_choch/discretion). |
| 3. Telegram alert | `_send_telegram` called on PENDING_NEW, OPEN, CLOSED events | ✅ WORKING | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set. Zero `telegram send failed` or `[no-telegram]` warnings in last 30d. 11 trade events fired alerts. |
| 4. User clicks link | Telegram message links to `http://100.120.181.11:3000/annotate/{id}` (Tailscale) | ⚠️ UNKNOWN — likely failure | No server-side click telemetry. **Inferred: low click rate.** With 95% empty thesis_pre and 100% empty lesson_post, user is either not seeing alerts or not clicking them. Tailscale URL requires VPN active on phone. |
| 5. Form loads | `/annotate/[id]/page.tsx` Next.js page | ✅ WORKING | Page loads `bybit/annotations/{id}` API. Rich context display (HTF, funding, OI, CVD, liq, VP, OB, SMC tags). |
| 6. Form submission | `PATCH /bybit/annotations/{id}` writes to DB | ✅ WORKING | `routes/bybit.py` exposes endpoint. The 2 thesis_pre rows in DB prove the write path works. |
| 7. Closure prompt | On CLOSED event, Telegram alert with link to lesson_post | ✅ WORKING | Logs show `CLOSED ... closure={'annotation_id': N, 'pnl_usd': ...}` followed by `_fmt_close_alert` Telegram. |

## Identified failure stages

**Primary failure: Stage 4 (discoverability + motivation gap).**
- Infra is sending alerts. User isn't acting on them.
- 95% of alerts result in zero data being entered.
- Probable reasons (non-exclusive):
  - Tailscale not always active on phone → link doesn't open
  - Notification swiped without reading
  - Form takes too long to fill (form has rich context display + 4 input fields; mobile scroll fatigue)
  - No consequence for skipping → no motivation

**Secondary failure: architectural mismatch with Rule 6.**
- Current flow is **POST-trade**: user opens position on Bybit → watcher detects → creates annotation row → sends link → user MIGHT fill it later.
- Rule 6 requires **PRE-trade**: user must fill thesis BEFORE placing the limit order. Form auto-rejects on bad emotional state. Without thesis = no order.
- The current architecture cannot enforce Rule 6 as written. Phase 2 must decide between two options:

  | Option | Description | Tradeoff |
  |--------|-------------|----------|
  | **A. New pre-trade flow** | User opens form FIRST in mobile → submits with planned entry/SL/TP/thesis → gets order_link_id → user places limit on Bybit using that ID → watcher matches by order_link_id. | Requires real workflow change. User must break habit of "Bybit first." High behavioral friction but correctly enforces Rule 6. |
  | **B. Post-trade auto-cancel forcing function** | Keep current flow. Add: if annotation thesis_pre is still empty 5 min after PENDING_NEW alert, watcher auto-cancels the limit order on Bybit. | Lower behavioral friction (he keeps placing orders the way he does). But means orders get cancelled without warning if he's slow — could miss fills he wanted. Enforces Rule 6 with consequence rather than prevention. |
  | **C. Hybrid** | Encourage flow A (pre-trade form) by making it the only path that pre-fills the order screen with correct prices via deep-link to Bybit app. Leave option B as fallback for orders placed directly. | Best UX but most engineering. Phase 2-3 work. |

## Open questions for user

Before Phase 2 design decision:
1. **Discoverability — when the Telegram alert fires, do you actually see and read it within ~5 minutes? Or does it sit in the notification stack ignored?**
2. **Tailscale on phone — is it always active, or do you turn it off sometimes? If off, the annotate link doesn't load.**
3. **Behavioral preference — Option A (you change habit and use form first) or Option B (you don't change habit but late annotations cancel orders)?**
4. **Do you actually want lesson_post (post-trade reflection) to be enforced too, or only thesis_pre (pre-trade)?** Lesson_post can't be pre-trade by definition.

## Phase 1 verdict

- **Infra is solid.** All 7 pipeline stages work technically.
- **Failure is human (Stage 4 → 6 abandonment).** No bug to fix. Need behavioral enforcement OR workflow redesign.
- **Architectural gap with Rule 6 identified.** Phase 2 cannot start until user picks A/B/C above.

## Recommended Phase 2 scope

Pending user choice on options:
- **If A:** Build new pre-trade form route + Bybit deep-link integration. ~1 week dev. Highest enforcement.
- **If B:** Add auto-cancel timer to watcher. ~1 day dev. Medium enforcement, possible UX regret.
- **If C:** Both. ~1.5 weeks. Best long-term.

Recommendation depends on user discipline confidence: if he's likely to remember the form, A. If not, B with auto-cancel as the forcing function.
