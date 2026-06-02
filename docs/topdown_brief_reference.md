# /topdown ICT Brief — Reference

Maps every element of the `/topdown` Telegram brief to its named ICT (Inner Circle Trader) concept and public source. Purpose: prove the brief is grounded in documented methodology, not invented. Pure SMC, zero classic indicators (RSI/ADX/Stoch/EMA-as-bias) per user preference.

**Code location:** `scripts/topdown_snapshot.py`
**Telegram handler:** `scripts/explain_bot.py` (`/topdown <pair>`)
**Branch:** `feat/topdown-ict-enhancements-phase1` off `feat/manual-edge-discipline-phase1`
**Implementation grill:** `docs/grill/_archive/topdown-ict-enhancements-2026-05-23.md`
**Implementation plan:** `docs/plans/_archive/topdown-ict-enhancements-2026-05-23.md`

---

## Concept map

| Brief element | ICT concept | What it measures | Public source |
|---|---|---|---|
| Cascade 4H → 1H → 30m → 15m | **Top-Down Analysis** | Bias propagates HTF → LTF; HTF anchors direction, LTF refines entry | ICT Core Content "Time and Price" module; ICT 2022 Mentorship |
| Weighted bias score (4H ×2, others ×1, total /5) | Top-Down weighted reconciliation | Single conviction read across the cascade | ICT Top-Down — HTF dominates by design |
| Displacement strength per TF (strong/moderate/weak) | **Displacement Candle** | Recent N-candle body magnitude vs prior baseline + close-to-extreme + direction consistency. Strong displacement = institutional commitment | ICT Mentorship 2022 "Market Maker Models" |
| PD Array position (premium/equilibrium/discount %) | **PD Array / Dealing Range** | Price location within HTF dealing range. >50% = premium (favor shorts), <50% = discount (favor longs), 50% ± band = equilibrium | ICT "Premium and Discount Arrays" series |
| IDM flag on last BOS | **Inducement (IDM)** | Opposite-side liquidity swept BEFORE the BOS = institutional bait preceding real move | ICT direct term — "IDM precedes the real move" |
| KEY ZONES — Order Blocks (pristine vs mitigated) | **Order Block (OB) / Mitigation Block** | Supply/demand candle leaving displacement behind. Pristine = unmitigated, strongest; mitigated = already touched, weaker | ICT "Order Block" core content + "Mitigation" theory |
| MAGNETS — unbroken BSL/SSL with touch count | **Buyside / Sellside Liquidity (BSL/SSL), Old Highs/Lows** | Resting stop clusters that act as magnets for price | ICT "Liquidity Pools" — exact terminology reused in `strategy_service/liquidity.py` |
| Equal-level cluster (touch_count ≥ 3) | **Engineered Liquidity** | Multiple touches build a magnet that institutions hunt before reversal | ICT "Liquidity Engineering" (Phase 2) |
| Killzone overlay (Asian / London / NY AM / NY PM) | **ICT Killzones** | Narrow UTC windows where institutional volatility concentrates. Asian 20:00–00:00, London 02:00–05:00, NY AM 12:00–15:00, NY PM 18:00–20:00 | ICT "Killzones" series — exact UTC windows |
| Invalidation (4H close beyond last swing) | **Market Structure Shift / CHoCH invalidation** | If 4H closes through the structure pivot that anchors current bias, the read is dead | ICT BOS vs CHoCH distinction |
| Bug fix: target ≥1.5R from sweep level | (Implementation safeguard) | Prevents "noise targets" — nearest unbroken liq picked regardless of distance. Floor = 1.5× (entry − invalidation) | Not an ICT concept — fix for SOL incident 2026-05-22 |

---

## Confidence ranges (existing `_reconcile` logic, kept unchanged)

| Confidence | Condition |
|---|---|
| `high` | Weighted score ≥ 4/5 — 4H aligned with ≥ 2 lower TFs |
| `medium` | Simple majority weighted vote, 4H may agree or be neutral |
| `low` | TFs split, 4H disagrees with majority, or ≥2 TFs undefined |

---

## Public sources (verifiable)

- **ICT YouTube official channel** — Top-Down Analysis playlist, Killzones series, PD Arrays series, Displacement and FVG videos. Free, multi-million views per video.
- **ICT 2022 / 2023 Mentorship** — formal series on Market Maker Models, Buyside/Sellside Liquidity, Inducement, Order Blocks vs Mitigation.
- **Maven Trading curriculum** — prop firm teaching ICT-derived SMC to funded traders. Same vocabulary.
- **The Trading Pit / FTMO** — prop desks accepting ICT/SMC trade thesis as valid for funded accounts.
- **TradingView "SMC Lux" indicator** — community indicator implementing the same concepts (OB, FVG, BOS, CHoCH, sweeps, PD). 100k+ active users.
- **Babypips "Smart Money Concepts" section** — retail-friendly intro using identical terminology.

---

## What this brief explicitly does NOT use

These were considered and rejected per `feedback_pure_smc_no_classic_indicators` (memory) and the 2026-05-23 grill:

- **RSI** — laggy in crypto regime shifts. Brief uses Displacement strength instead.
- **ADX** — trend-strength via indicator. Brief uses Displacement consistency instead.
- **Bollinger Bands, MACD, Stochastic** — derivative indicators. Brief sticks to price action.
- **EMA as bias source** — Rules taxonomy v3 mentions 4H 50 EMA as a filter, but brief uses BOS/CHoCH-derived structure as primary bias source. EMA may surface as confirmation in a later phase, never as standalone signal.

Bot internal ML feature collection (`shared/ml_features.py` v13/v15/v16) still includes RSI, WaveTrend, ADX, BBW, StochRSI — those are for offline modeling, NOT human-facing manual-trading output. Separation is intentional.

---

## Phase status

- **Phase 1** (this doc) — bug fix + 4 ICT helpers (Displacement / PD Array / IDM / Killzones) + Telegram-Markdown reformat + `topdown_brief_renders` usage tracking
- **Phase 2** (blocked ≥2 weeks post-P1) — OB pristine/mitigated full surface, unfilled FVG magnets section, equal-highs/lows engineered liquidity flag
- **Phase 3** (blocked N≥20 in both falsification buckets) — WR-delta gate via JOIN of `bybit_trade_annotations` to `topdown_brief_renders` within 30-min window. Decision: KEEP / EXTEND / KILL.

See `docs/plans/_archive/topdown-ict-enhancements-2026-05-23.md` for full plan + verification gates.
