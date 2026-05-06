# Engine1 Maker Fill-Rate Study — 2026-05-05

**Status:** Investigation complete. Recommendation: **HOLD engine1 live promotion.**
**Author:** Claude Code (per user request, conversational session 2026-05-05)
**Branch:** `feat/engine1-maker-fillrate-study`
**Script:** `scripts/engine1_fillrate_study.py`
**Sample:** 37 ETH/USDT short engine1 shadow setups, 2026-04-28 → 2026-05-02
**Experiment:** `redesign_pre_2026_04_27`, feature_version 18

---

## 1. Question

Last EV computation showed engine1 ETH-short net PnL is +$6.66 over 37 resolved
trades — gross PF 1.14, net PF 0.88 if fees were re-applied — wide CI straddling
zero. The conjecture: switching live execution from market (taker) to `post_only`
limit (maker) reduces fees from 0.10% RT to 0.04–0.07% RT and could flip the
edge cleanly positive.

**Two unknowns block the decision:**
1. What fraction of historical engine1 detections would have been **accepted as
   `post_only`** at placement time (i.e., entry_price had not already been
   crossed by current market price)?
2. For those that placed successfully, what is the **realistic fill rate** under
   different queue-depth assumptions (a wick that taps the level briefly may
   not fill a maker behind a queue)?

If the combined effective fill rate is high enough, the post-fee edge survives.
If not, engine1 is not viable as a live setup at this sample size.

---

## 2. Method

The script (`scripts/engine1_fillrate_study.py`) replays the 37 setups against
the persisted `candles` table at 5m resolution:

**Step A — `post_only` validity at placement:**
For each setup, fetch the 5m candle at-or-before `created_at`. Compare its close
to `entry_price`. A short sell limit is `post_only` valid only if
`entry_price > ref_close` (limit must sit above market). Otherwise post_only is
rejected (entry already crossed at detection time = adverse setup).

**Step B — fill within the entry timeout window:**
For each placed setup, walk forward 5m candles until
`SHADOW_ENTRY_TIMEOUT_HOURS = 12` (current setting). Apply five queue-depth
margins: 0, 1, 3, 5, 10 bps. A short fills when
`high >= entry_price * (1 + margin/10000)`.

The 0bps case matches `_candle_touched_price` — what the existing shadow
already assumes. The higher-margin cases approximate increasingly conservative
queue-position assumptions: a 5bps margin on $2300 ETH means the wick must
extend $1.15 past the limit before we count the fill — a realistic guard
against partial-queue execution.

**Step C — fee re-projection:**
For the filled subset under each margin, back out gross PnL from the existing
taker-net `pnl_usd` (shadow's `compute_pnl` already deducts
`TRADING_FEE_RATE = 0.0005` per side). Re-apply alternative fee models:
maker-entry+taker-exit (0.07% RT), maker+maker (0.04% RT).

---

## 3. Results

### 3.1 `post_only` placement validity

| Setups | Posted (entry not yet crossed) | Rejected |
|---|---|---|
| 37 | **29 (78.4%)** | 8 (21.6%) |

Eight setups had `entry_price` already crossed by 5m close at detection. Those
would have been refused by `post_only` and effectively skipped live.

### 3.2 Fill rate within 12h, by queue margin

| Margin (bps) | Fills / Posted | Fill rate | Avg fill latency (5m bars) |
|---|---|---|---|
| 0 | 27 / 29 | **93.1%** | 10.3 (~52 min) |
| 1 | 27 / 29 | 93.1% | 10.6 |
| **3** | **25 / 29** | **86.2%** | 11.6 |
| 5 | 25 / 29 | 86.2% | 12.7 |
| 10 | 25 / 29 | 86.2% | 18.0 |

The cliff between 1 and 3 bps removes 2 setups. Those 2 setups carry the edge.

### 3.3 Net PnL by margin × fee model

Filled subset only. `gross_sum` backs out current taker-x2 fee. `tk_net` = same
sample re-applied at taker-x2 (0.10% RT). `mk-tk_net` = maker entry + taker
exit (0.07% RT). `mk-mk_net` = maker both sides (0.04% RT).

| Margin | Fills | Gross sum | Taker net | Maker+Taker net | Maker+Maker net |
|---|---|---|---|---|---|
| 0 bps | 27 | **+$19.01** | +$11.12 | +$13.49 | **+$15.86** |
| 1 bps | 27 | +$19.01 | +$11.12 | +$13.49 | +$15.86 |
| **3 bps** | **25** | **−$1.00** | **−$7.35** | **−$5.45** | **−$3.54** |
| 5 bps | 25 | −$1.00 | −$7.35 | −$5.45 | −$3.54 |
| 10 bps | 25 | −$1.00 | −$7.35 | −$5.45 | −$3.54 |

**Baseline (all 37 unfiltered):** gross +$20.07, taker net +$6.66.

### 3.4 Edge concentration — the dominant finding

Pulling the trade ledger by P&L:

**Top 5 winners** (sum +$45.81, all `shadow_tp`):
- 2026-04-29 14:15–15:05 UTC, all ETH entry ~$2302–2305, all ~+$9.10 each.
  These are five separate `setup_id`s detected within a 50-minute window during
  one continuous downward move. The dedup gate failed to suppress all but the
  first because each new 5m bar reset the signal.

**Top 5 losers** (sum −$28.91, four `shadow_sl`):
- 2026-05-01 21:30–22:00 UTC, four entries all at $2290.505, all SL'd at $2304.9.
  Same signal firing four times in 30 minutes against a single losing event.

**Implication:** the headline N=37 is misleading. After collapsing time-clustered
duplicates, the **effective independent-event N is roughly 10–15**. The
positive headline gross is one or two market events away from flipping
negative — and step 3.2 shows that's exactly what happens when 2 wick-only
fills get pruned by realistic queue assumptions.

---

## 4. Verdict

**Engine1 should NOT be promoted to live trading at this sample size.**

Reasons, in priority order:

1. **Edge is event-clustered.** Of N=37, roughly 10–15 are independent market
   events. The single large-winner cluster on 2026-04-29 alone contributes
   ~$45 of the +$20 gross. The signal survives statistical scrutiny only when
   that one event is included with all 5 sub-detections counted as fills.
2. **Queue-realistic fill model wipes out the edge.** A 3 bps margin
   requirement removes only 2 of 27 fills but flips the gross from +$19 to
   −$1. Edge depends on filling wick-only touches, which is exactly the
   scenario where post-only queue priority hurts most.
3. **Sample is starved.** N=37 (effective ~10–15) is below the original
   promotion gate of 50 resolved. Wide bootstrap CI on every fee scenario.
4. **Eight setups (21.6%) would have been refused by post-only at placement.**
   This is not a bug — it correctly skips adverse entries — but it further
   reduces effective volume.

---

## 5. Path forward

Three options, in order of recommendation:

**Option 1 — Continue shadow, raise the bar.** (RECOMMENDED)
- Wait for **N≥75 resolved** ETH-short outcomes (currently 37; ~5 weeks at
  current rate of ~7/week)
- **Fix the dedup gap** so cluster-detections collapse to one outcome. Current
  setting allows the same engine1 signal to re-fire every 5m within a window;
  this inflates apparent sample and concentrates edge in a single market
  event. Tighten to one terminal outcome per impulse-pullback cycle.
- Re-run this study after fixing dedup. If post-dedup gross PF still ≥ 1.2 at
  3 bps margin, proceed to Option 3.

**Option 2 — Add 1m candles for sharper fill resolution.**
- Current 5m model can't distinguish wick-tap from sustained touch. 1m would
  show whether high prints sat at level for 1+ minute (queue-clearing) or
  ticked through in seconds.
- Cost: enable 1m on `LTF_TIMEFRAMES`, backfill 30d for ETH/USDT only
  (~5MB). Update study to use 1m.
- Decision criterion: if 1m study shows wick-tap fills (<1min residence) are
  responsible for the 2 critical winners, edge is fragile. If sustained
  touches, edge is more robust.

**Option 3 — If Options 1+2 confirm edge survives.**
- Implement `MAKER_ONLY_SETUPS = ["engine1_trend_pullback"]` in
  `config/settings.py`.
- Add `post_only=True` path in `executor.py` — reject and skip on placement
  rejection (do not fall back to taker). Cancel + re-skip on entry timeout.
- Live with `LIVE_SETUP_RISK_OVERRIDES["engine1_trend_pullback"] = 0.002`
  ($1 risk on $86 capital) for first 20 trades.
- Compare live fill rate to study estimates. If realized fill rate <70% or
  net PF <1.0 over 20 trades → revert to shadow.

---

## 6. Followup work captured

- [ ] Audit dedup logic for engine1 — why do 5 detections fire within 50 min on
  the same impulse?
- [ ] Add 1m candle persistence for ETH/USDT (+ BTC if engine1 is re-extended).
- [ ] Re-run this study after dedup fix + 1m data, expected after N≥50 fresh
  shadow rows under tightened dedup.
- [ ] If kill: archive engine1 from `SHADOW_MODE_SETUPS`, free shadow-monitor
  capacity for scalp v2 follow-on.

---

## 7. Reproducibility

```bash
# from repo root
PYTHONPATH=. python scripts/engine1_fillrate_study.py
```

Required state:
- PostgreSQL running with `ml_setups` populated under
  `experiment_id=redesign_pre_2026_04_27`
- `candles` table containing ETH/USDT 5m for 2026-04-28 through 2026-05-02 (current coverage extends to 2026-05-06)
