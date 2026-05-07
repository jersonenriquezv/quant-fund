"""Why are scalp_liq_reclaim_v1 and scalp_funding_extreme_v1 silent?

Phase 1A followup. Both detectors have produced 0 outcomes ever despite the
bot running and other scalp signals firing. This script tests whether the
gates would have fired given the data the bot already has access to.

For funding_extreme: pull 30 days of `funding_rate_history`, count how many
samples pass `|rate| >= _FUNDING_RATE_THRESHOLD` (default 0.05%) at the
current threshold and at proposed alternates.

For liq_reclaim: walk through `open_interest_history`, find historical OI
drops that match `OI_DROP_THRESHOLD_PCT`/`OI_DROP_WINDOW_SECONDS`, then for
each flush check whether the 5m candle window contained a setup that would
have passed the wick + inside-range gates.

Run:
  PYTHONPATH=. python scripts/scalp_silent_detector_audit.py
"""

from __future__ import annotations

import psycopg2

from config.settings import settings


# Replicate detector constants — keep in sync with strategy_service/scalp_setups.py
LIQ_RECLAIM_WICK_THRESHOLD = 0.005       # 0.5%
LIQ_RECLAIM_LOOKBACK_BARS = 20
LIQ_RECLAIM_FLUSH_MAX_AGE_MS = 5 * 60 * 1000

FUNDING_RATE_THRESHOLD = 0.0005          # 0.05% per 8h
FUNDING_FLAT_LOOKBACK_BARS = 6
FUNDING_FLAT_RANGE_THRESHOLD = 0.003     # 0.3%

DAYS = 30


def main() -> None:
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )
    cur = conn.cursor()

    print("=" * 96)
    print("SCALP SILENT DETECTOR AUDIT — liq_reclaim + funding_extreme")
    print("=" * 96)

    # -----------------------------------------------------------------
    # FUNDING_EXTREME
    # -----------------------------------------------------------------
    print(f"\n[A] funding_extreme — threshold |rate| >= {FUNDING_RATE_THRESHOLD*100:.3f}% (8h)")
    print("-" * 96)
    cur.execute(
        f"""
        SELECT pair,
               COUNT(*) as samples,
               COUNT(*) FILTER (WHERE ABS(rate) >= 0.0005) as hits_0p050,
               COUNT(*) FILTER (WHERE ABS(rate) >= 0.0003) as hits_0p030,
               COUNT(*) FILTER (WHERE ABS(rate) >= 0.0002) as hits_0p020,
               COUNT(*) FILTER (WHERE ABS(rate) >= 0.0001) as hits_0p010,
               ROUND(MAX(ABS(rate))::numeric, 6) as max_abs,
               ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ABS(rate))::numeric, 6) as p95_abs,
               ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ABS(rate))::numeric, 6) as p99_abs
        FROM funding_rate_history
        WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '{DAYS} days') * 1000
        GROUP BY 1
        ORDER BY 1
        """
    )
    rows = cur.fetchall()
    print(f"{'pair':<12}{'N':>5}{'>=0.05%':>9}{'>=0.03%':>9}{'>=0.02%':>9}{'>=0.01%':>9}{'p95|%|':>11}{'p99|%|':>11}{'max|%|':>11}")
    total_hits_0p050 = 0
    total_hits_0p030 = 0
    total_hits_0p020 = 0
    total_hits_0p010 = 0
    total_n = 0
    for pair, n, h050, h030, h020, h010, mx, p95, p99 in rows:
        total_hits_0p050 += h050; total_hits_0p030 += h030; total_hits_0p020 += h020; total_hits_0p010 += h010
        total_n += n
        print(f"{pair:<12}{n:>5}{h050:>9}{h030:>9}{h020:>9}{h010:>9}{p95*100:>10.4f}%{p99*100:>10.4f}%{mx*100:>10.4f}%")
    print("-" * 96)
    print(f"{'TOTAL':<12}{total_n:>5}{total_hits_0p050:>9}{total_hits_0p030:>9}{total_hits_0p020:>9}{total_hits_0p010:>9}")
    print(f"\nDIAGNOSIS:")
    if total_hits_0p050 == 0:
        print(f"  Threshold 0.05% impossible. 30d MAX observed funding < threshold.")
        print(f"  Detector cannot fire under current setting. ROOT CAUSE: threshold mis-calibration.")
    print(f"\nProposed thresholds and 30d firing capacity:")
    print(f"  0.05% (current) -> {total_hits_0p050} hits, ~{total_hits_0p050/DAYS:.1f}/day")
    print(f"  0.03%           -> {total_hits_0p030} hits, ~{total_hits_0p030/DAYS:.1f}/day")
    print(f"  0.02%           -> {total_hits_0p020} hits, ~{total_hits_0p020/DAYS:.1f}/day")
    print(f"  0.01%           -> {total_hits_0p010} hits, ~{total_hits_0p010/DAYS:.1f}/day")
    print(f"  Note: gate also requires flat range (0.3% over 30min) - real fires << threshold hits.")

    # -----------------------------------------------------------------
    # LIQ_RECLAIM
    # -----------------------------------------------------------------
    print(f"\n[B] liq_reclaim — OI drop >= {settings.OI_DROP_THRESHOLD_PCT*100:.1f}% in {settings.OI_DROP_WINDOW_SECONDS}s")
    print("-" * 96)
    # Detect historical OI flushes per pair using window logic similar to OIFlushDetector
    cur.execute(
        f"""
        WITH oi AS (
          SELECT pair, timestamp, oi_usd,
                 LAG(oi_usd) OVER (PARTITION BY pair ORDER BY timestamp) as prev_oi,
                 LAG(timestamp) OVER (PARTITION BY pair ORDER BY timestamp) as prev_ts
          FROM open_interest_history
          WHERE timestamp >= EXTRACT(EPOCH FROM NOW() - INTERVAL '{DAYS} days') * 1000
            AND oi_usd > 0
        )
        SELECT pair, timestamp,
               ROUND(((prev_oi - oi_usd)/prev_oi * 100)::numeric, 2) as drop_pct,
               oi_usd, prev_oi
        FROM oi
        WHERE prev_oi > 0
          AND timestamp - prev_ts <= 600000
          AND (prev_oi - oi_usd)/prev_oi >= {settings.OI_DROP_THRESHOLD_PCT}
        ORDER BY pair, timestamp
        """
    )
    flushes = cur.fetchall()
    flushes_by_pair: dict[str, list] = {}
    for pair, ts, drop_pct, oi_now, oi_prev in flushes:
        flushes_by_pair.setdefault(pair, []).append((ts, float(drop_pct), float(oi_now), float(oi_prev)))

    print(f"Historical OI flushes (>= {settings.OI_DROP_THRESHOLD_PCT*100:.1f}% drop in <= 10min):")
    print(f"{'pair':<12}{'flushes':>9}")
    total_flushes = 0
    for pair, fl in flushes_by_pair.items():
        print(f"{pair:<12}{len(fl):>9}")
        total_flushes += len(fl)
    print(f"{'TOTAL':<12}{total_flushes:>9}  ({total_flushes/DAYS:.1f}/day)")

    # For each flush, find the 5m candles within the next LIQ_RECLAIM_FLUSH_MAX_AGE_MS
    # window for the same pair, and test the wick + inside-range gates.
    print(f"\nGate alignment test — for each flush, do the next 5m candles satisfy:")
    print(f"  (a) wick >= {LIQ_RECLAIM_WICK_THRESHOLD*100:.1f}%   (b) close inside prior {LIQ_RECLAIM_LOOKBACK_BARS}-bar range")
    print(f"  Window: {LIQ_RECLAIM_FLUSH_MAX_AGE_MS/60000:.0f} min after flush.")
    print()
    print(f"{'pair':<12}{'flushes':>9}{'aligned':>9}{'wick_only':>11}{'rate':>8}")
    total_aligned = 0
    total_wickonly = 0
    for pair, fl in flushes_by_pair.items():
        aligned = 0
        wick_only = 0
        for ts, _, _, _ in fl:
            window_end = ts + LIQ_RECLAIM_FLUSH_MAX_AGE_MS
            cur.execute(
                """
                SELECT timestamp, open, high, low, close
                FROM candles
                WHERE pair = %s AND timeframe = '5m'
                  AND timestamp BETWEEN %s AND %s
                ORDER BY timestamp
                """,
                (pair, ts, window_end),
            )
            window_candles = cur.fetchall()
            for c_ts, c_o, c_h, c_l, c_c in window_candles:
                c_o, c_h, c_l, c_c = float(c_o), float(c_h), float(c_l), float(c_c)
                if c_c <= 0:
                    continue
                body_top = max(c_o, c_c)
                body_bottom = min(c_o, c_c)
                upper_wick = max(0.0, c_h - body_top)
                lower_wick = max(0.0, body_bottom - c_l)
                upper_pct = upper_wick / c_c
                lower_pct = lower_wick / c_c
                wick_passes = (
                    (lower_pct >= LIQ_RECLAIM_WICK_THRESHOLD and lower_pct > upper_pct)
                    or (upper_pct >= LIQ_RECLAIM_WICK_THRESHOLD and upper_pct > lower_pct)
                )
                if not wick_passes:
                    continue
                wick_only += 1
                # Inside-range gate — pull prior 20 candles BEFORE this candle
                cur.execute(
                    """
                    SELECT high, low FROM candles
                    WHERE pair = %s AND timeframe = '5m' AND timestamp < %s
                    ORDER BY timestamp DESC LIMIT %s
                    """,
                    (pair, c_ts, LIQ_RECLAIM_LOOKBACK_BARS),
                )
                prior = cur.fetchall()
                if len(prior) != LIQ_RECLAIM_LOOKBACK_BARS:
                    continue
                p_high = max(float(p[0]) for p in prior)
                p_low = min(float(p[1]) for p in prior)
                if p_low <= c_c <= p_high:
                    aligned += 1
                    break  # only count one alignment per flush (first matching candle)
        total_aligned += aligned
        total_wickonly += wick_only
        print(f"{pair:<12}{len(fl):>9}{aligned:>9}{wick_only:>11}{aligned/len(fl)*100 if fl else 0:>7.1f}%")
    print("-" * 96)
    print(f"{'TOTAL':<12}{total_flushes:>9}{total_aligned:>9}{total_wickonly:>11}")

    print(f"\nDIAGNOSIS:")
    if total_aligned == 0:
        print(f"  Zero historical alignment despite {total_flushes} flushes. Gates incompatible.")
        print(f"  Likely cause: 0.5% wick threshold + inside-range too strict for a 2% OI flush.")
    elif total_aligned > 0 and total_aligned < 5:
        print(f"  {total_aligned} historical alignments in {DAYS} days = effectively too rare.")
        print(f"  Consider relaxing wick threshold or extending flush window.")
    else:
        print(f"  {total_aligned} historical alignments in {DAYS} days = ~{total_aligned/DAYS:.1f}/day.")
        print(f"  Detector is well-calibrated. ROOT CAUSE: runtime issue (OI flush detector ")
        print(f"  not initialized, snapshot missing recent_oi_flushes, or master switch off).")

    # -----------------------------------------------------------------
    # SUMMARY
    # -----------------------------------------------------------------
    print(f"\n" + "=" * 96)
    print("ACTION PLAN")
    print("=" * 96)
    print(f"\nfunding_extreme:")
    if total_hits_0p050 == 0:
        recommended = 0.0002 if total_hits_0p020 >= DAYS * 2 else 0.0001
        print(f"  Lower _FUNDING_RATE_THRESHOLD from 0.0005 to {recommended} ({recommended*100:.2f}%).")
        print(f"  Bump SCALP_EXPERIMENT_ID so old (no-fire) data stays separate.")
    print(f"\nliq_reclaim:")
    print(f"  If alignment > 0: investigate runtime — check OI flush detector init, snapshot wiring.")
    print(f"  If alignment = 0: tune gates. Options:")
    print(f"    - Lower wick threshold from 0.5% to 0.3%")
    print(f"    - Drop inside-range gate (it may be incompatible with flush dynamics)")
    print(f"    - Extend flush window from 5min to 10min")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
