# HTF Position Trading + Pyramid Adds

## What

Add a second trading mode: HTF swing/position trades on 4H/1H candles that hold for days/weeks, with pyramid scaling (adding to winners as trend confirms). Inspired by Tom Hougaard's "Best Loser Wins" — cut losers fast, add to winners aggressively.

## Why

Current bot only does intraday (5m/15m). Problems:
- **BTC barely trades** — 5m OBs are too narrow relative to $70K price. 4H OBs ($700-$2000 wide) solve this structurally.
- **Missing the big moves** — 5m trades capture $50-$150 swings. A 4H trend continuation captures $2,000-$5,000+ on BTC.
- **Pyramid = asymmetric R:R** — Initial risk is small, but if the trend runs and you add 2-3x, a single campaign can return 5:1 to 10:1 on the original risk.
- **Higher conviction** — HTF patterns are more reliable (less noise, more institutional participation).

## Current State (verified by reading code)

- **Trigger timeframes**: 5m/15m candles only (`LTF_TIMEFRAMES`)
- **Trend timeframes**: 4H/1H used ONLY for bias (`HTF_TIMEFRAMES`)
- **Position model**: `ManagedPosition` = single entry per pair, keyed by pair in `_positions` dict
- **Position sizing**: Fixed $20 margin × 5x leverage = $100 notional per trade
- **Max duration**: 12 hours (`MAX_TRADE_DURATION_SECONDS = 43200`)
- **Exit**: Single TP at 2:1 R:R, breakeven at 1:1, trailing SL at 1.5:1
- **No concept of**: position campaigns, pyramid adds, multi-entry groups, HTF-specific risk limits

## Design

### Two Trading Modes (coexist)

| | Intraday (current) | HTF Position (new) |
|---|---|---|
| **Trigger candles** | 5m, 15m | 4H, 1H |
| **Trend bias from** | 4H/1H | Daily/Weekly |
| **Setups** | A, B, D, F (same SMC) | A, B, F (same SMC logic, bigger timeframe) |
| **Hold time** | Hours (max 12h) | Days/weeks (max 7 days, extendable) |
| **Entry** | Limit at OB 50% | Limit at OB 50% (4H OB) |
| **SL** | Below OB (5m/15m) | Below OB (4H/1H) — wider, $500-$2000 |
| **TP** | Fixed 2:1 R:R | Trailing only (no fixed TP — let winners run) |
| **Pyramid adds** | No | Yes — up to 3 adds |
| **AI filter** | Yes (Sonnet) | Yes (different prompt — macro focus) |
| **Margin** | $20 fixed | $30 initial, $15/$10/$5 adds |
| **Max campaigns** | N/A (independent positions) | 1 per pair at a time |

### Pyramid Logic (Hougaard-style)

```
CAMPAIGN LIFECYCLE:

1. INITIAL ENTRY
   - 4H Setup A/B/F fires (same SMC patterns on higher timeframe)
   - AI approves with macro context (Daily trend, weekly structure)
   - Entry: limit at 4H OB entry price
   - SL: below 4H OB (campaign SL)
   - No fixed TP — trail only
   - Margin: $30

2. ADD #1 — Trend Confirms (1:1 R:R reached)
   - Price has moved 1:1 R:R in favor
   - New 4H BOS confirms direction
   - New 4H OB forms near current price → add entry
   - Campaign SL moves to initial entry (breakeven on original)
   - Margin: $15 (half of initial)

3. ADD #2 — Trend Extends (2:1 R:R from initial)
   - Price has moved 2:1 R:R from original entry
   - Another 4H BOS confirms continuation
   - Campaign SL moves to Add #1 entry (lock profit)
   - Margin: $10

4. ADD #3 — Final (3:1 R:R from initial, optional)
   - Only if trend is exceptionally strong
   - Campaign SL at Add #2 entry
   - Margin: $5
   - This is the smallest add — pyramid shape

TOTAL MAX CAMPAIGN: $60 margin × 5x = $300 notional
(At $108 capital this is ~55% allocation — aggressive but controlled)
```

### Campaign SL Management

```
Instead of fixed TP, use structure-based trailing:

- SL always below the most recent 4H swing low (for longs)
- When a new higher low forms on 4H → SL moves up
- Campaign invalidation: 4H CHoCH against direction → close everything

This is the key difference from intraday:
- Intraday: fixed TP at 2:1 R:R
- HTF: NO fixed TP, trail with structure. Let the trend run.
```

### Capital Allocation Guard

```
At $108 capital, BOTH modes can't run at full capacity:

HTF campaign running ($60 margin) → only $48 left for intraday
   → Reduce MAX_OPEN_POSITIONS for intraday from 5 to 2

No HTF campaign → full intraday capacity (5 positions, $20 each)

This is managed by a CapitalAllocator that distributes between modes.
```

## Steps

### Phase 1: HTF Setup Detection
1. **Add Daily/Weekly candle collection** → `data_service/` → WebSocket already handles multi-TF, just add "1D" to backfill and subscriptions. Done when 4H/1H candles trigger pipeline + Daily for bias.
2. **HTF strategy evaluation** → `strategy_service/service.py` → Add `evaluate_htf()` method that runs A/B/F on 4H/1H candles using Daily bias. Done when 4H setups detected in logs.
3. **HTF-specific AI prompt** → `ai_service/prompt_builder.py` → Add macro context section (Daily structure, Weekly bias). Done when prompt includes "position trade" context.

### Phase 2: Campaign Position Management
4. **Campaign model** → `execution_service/models.py` → Add `PositionCampaign` dataclass that groups entries. Done when campaign tracks initial + adds.
5. **Pyramid trigger detection** → `strategy_service/service.py` → Add `check_pyramid_add()` — new BOS + OB on 4H while campaign active. Done when add signals logged.
6. **Campaign monitor** → `execution_service/monitor.py` → Add campaign lifecycle (structure-based trailing SL, add execution, campaign close). Done when campaign SL trails in logs.

### Phase 3: Risk & Capital
7. **Capital allocator** → `risk_service/` → New module that tracks HTF vs intraday budget. Done when intraday capacity reduces when HTF campaign active.
8. **Campaign-level guardrails** → `risk_service/guardrails.py` → Max 1 campaign per pair, max campaign exposure, add sizing rules. Done when guardrails logged for campaigns.

### Phase 4: Dashboard & DB
9. **DB schema** → Add `campaigns` table linking trades. Done when campaign history queryable.
10. **Dashboard** → Show active campaigns, add history, campaign P&L. Done when visible on dashboard.

## Risks

| Risk | Mitigation |
|---|---|
| $108 is too small for meaningful pyramiding | Start with $30 initial only (no adds). Enable adds when capital > $300. |
| HTF position + intraday on same pair → margin conflict | OKX allows only 1 position per pair in isolated mode. HTF and intraday must be on DIFFERENT pairs, or use cross-margin for HTF. Verify with OKX API first. |
| Campaign SL trail too tight → whipsawed out | Trail on 4H swing lows only (not 1H). 4H swings are stable. |
| Adding to winner at the top → all adds lose | Each add is smaller (pyramid shape). Worst case: initial breaks even, adds lose small. Max campaign loss = initial risk only (SL moved to BE before first add). |
| Correlated campaigns BTC + ETH (r=0.85) | Max 1 active campaign total (not per pair). If BTC campaign active, no ETH campaign. |
| Longer hold = more exposure to black swans | Campaign SL is always live on exchange. 7-day max before forced review. |

## Out of Scope

- **Cross-margin mode** — Keep isolated margin for now. Simpler risk model.
- **Partial add fills** — First version: add is all-or-nothing.
- **Automated campaign opening** — Phase 1 could be semi-manual: bot detects HTF setup, sends Telegram alert, user confirms before executing. Full auto in Phase 2.
- **Multiple simultaneous campaigns** — At $108 capital, max 1 campaign at a time. Add multi-campaign when capital > $1,000.
- **Altcoins** — HTF only on BTC and ETH. No altcoin position trades.

## Capital Considerations

With $108 this feature is limited but still valuable because:
1. Even without adds, HTF entries on 4H OBs solve the "BTC never trades" problem (wider SLs, proper OB sizes)
2. Structure-based trailing (no fixed TP) captures the full move instead of exiting at 2:1
3. Pyramiding becomes meaningful at $300+ capital — but the infrastructure should be built now

Recommended rollout:
- **Phase 1 only** at current capital: HTF setup detection + single entry (no pyramid yet)
- **Phase 2 (pyramid)** when capital reaches $300
- **Phase 3 (multi-campaign)** when capital reaches $1,000

## Open Questions for User

1. **Semi-auto vs full-auto?** Should HTF trades auto-execute like intraday, or should the bot detect + alert and you confirm via Telegram?
2. **Max 1 campaign total or 1 per pair?** At $108 probably 1 total. At $300+ could be 1 per pair.
3. **Priority**: Should we build Phase 1 only (HTF detection + single entry, no pyramid) and validate before adding pyramid logic?
