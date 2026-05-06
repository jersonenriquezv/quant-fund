"""Scalp fee viability analysis — Phase 1A.

For each scalp signal in `SCALP_SIGNAL_PARAMS`, compute the breakeven win-rate
required for positive expectancy under three fee models, then compare against
observed v1 outcomes from `ml_setups`.

Fee models (round-trip):
  - taker+taker  = 0.10% RT (current default; OKX VIP-0 SWAP: 0.05% per leg)
  - maker+taker  = 0.07% RT (post_only entry, market exit on TP/SL)
  - maker+maker  = 0.04% RT (post_only entry AND post_only exit; SL still
                              market — see note)

Outputs per signal:
  - Breakeven WR for binary TP/SL outcomes (no BE / no time-stop)
  - Breakeven WR including realized BE and time-stop drag
  - Observed WR (TP / TP+SL+BE+TS)
  - Verdict: VIABLE / MARGINAL / KILL per fee model

Run:
  PYTHONPATH=. python scripts/scalp_fee_viability.py
"""

from __future__ import annotations

import psycopg2

from config.settings import settings


# Fee models
FEE_MODELS = {
    "taker+taker (0.10% RT)": 0.0010,
    "maker+taker (0.07% RT)": 0.0007,
    "maker+maker (0.04% RT)": 0.0004,
}


def breakeven_wr_binary(tp_pct: float, sl_pct: float, fee_rt: float) -> float:
    """Breakeven win-rate assuming only TP or SL outcomes.

    EV = wr × (tp - fee) - (1-wr) × (sl + fee) = 0
    => wr = (sl + fee) / (tp + sl)
    """
    return (sl_pct + fee_rt) / (tp_pct + sl_pct)


def breakeven_wr_with_dist(
    tp_pct: float,
    sl_pct: float,
    fee_rt: float,
    p_be: float,
    p_ts: float,
    avg_ts_pct: float,
) -> float:
    """Breakeven win-rate when BE and time-stop outcomes are present.

    Among non-TP, non-SL trades: BE pays roughly -fee (round-trip cost only)
    because price returned to entry; time-stops resolve at the close after
    `time_stop_seconds`, modeled here at avg_ts_pct (signed, gross of fees).

    Conditional EV:
      EV = p_tp × (tp - fee)
         + p_sl × -(sl + fee)
         + p_be × (-fee)               # BE: zero gross, fees applied
         + p_ts × (avg_ts_pct - fee)   # avg_ts_pct already net? we treat as gross then re-apply fee

    Constraint: p_tp + p_sl + p_be + p_ts = 1 → p_sl = 1 - p_tp - p_be - p_ts.

    Solving for p_tp such that EV = 0:
      0 = p_tp × (tp + sl) - (1 - p_be - p_ts) × (sl + fee)
        + p_be × (-fee) + p_ts × (avg_ts_pct - fee)
      p_tp × (tp + sl) = (1 - p_be - p_ts) × (sl + fee) + p_be × fee
                        - p_ts × (avg_ts_pct - fee)
    """
    rhs = (1.0 - p_be - p_ts) * (sl_pct + fee_rt) + p_be * fee_rt - p_ts * (avg_ts_pct - fee_rt)
    return rhs / (tp_pct + sl_pct)


def fetch_observed(cur, setup_type: str) -> dict:
    """Aggregate observed outcomes across all experiments for a setup."""
    cur.execute(
        """
        SELECT outcome_type, COUNT(*),
               AVG(pnl_pct) as avg_pnl_pct
        FROM ml_setups
        WHERE setup_type = %s
          AND outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven','shadow_time_stop','shadow_timeout')
        GROUP BY 1
        """,
        (setup_type,),
    )
    rows = cur.fetchall()
    out = {"shadow_tp": 0, "shadow_sl": 0, "shadow_breakeven": 0,
           "shadow_time_stop": 0, "shadow_timeout": 0,
           "avg_ts_pct": 0.0}
    ts_total_pct = 0.0
    ts_total_n = 0
    for outcome, n, avg_pct in rows:
        out[outcome] = n
        if outcome in ("shadow_time_stop", "shadow_timeout") and avg_pct is not None:
            ts_total_pct += float(avg_pct) * n
            ts_total_n += n
    if ts_total_n > 0:
        out["avg_ts_pct"] = ts_total_pct / ts_total_n
    return out


def main() -> None:
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )
    cur = conn.cursor()

    print("=" * 96)
    print("SCALP FEE VIABILITY — PHASE 1A")
    print("=" * 96)
    print(f"\nFee models considered:")
    for label, rt in FEE_MODELS.items():
        print(f"  {label:<30} = {rt*100:.2f}% round-trip")
    print()

    print("PART 1 — Theoretical breakeven WR (binary TP/SL only, no BE/TS)")
    print("-" * 96)
    print(f"{'signal':<30}{'TP%':>6}{'SL%':>6}{'R:R':>6}", end="")
    for label in FEE_MODELS:
        print(f"  {label.split()[0][:8]:>10}", end="")
    print()

    for setup_type, params in settings.SCALP_SIGNAL_PARAMS.items():
        tp = params["tp_pct"] / 100.0
        sl = params["sl_pct"] / 100.0
        rr = tp / sl if sl > 0 else float("inf")
        print(f"{setup_type:<30}{params['tp_pct']:>6.2f}{params['sl_pct']:>6.2f}{rr:>6.2f}", end="")
        for label, fee_rt in FEE_MODELS.items():
            wr = breakeven_wr_binary(tp, sl, fee_rt)
            print(f"  {wr*100:>9.1f}%", end="")
        print()
    print()

    print("PART 2 — Observed v1 outcomes per signal (any experiment_id)")
    print("-" * 96)
    print(f"{'signal':<30}{'TP':>4}{'SL':>4}{'BE':>4}{'TS':>4}{'TO':>4}{'N':>5}{'TP-rate':>9}{'SL-rate':>9}{'avg_TS%':>10}")
    obs_data: dict[str, dict] = {}
    for setup_type in settings.SCALP_SIGNAL_PARAMS:
        obs = fetch_observed(cur, setup_type)
        obs_data[setup_type] = obs
        n = obs["shadow_tp"] + obs["shadow_sl"] + obs["shadow_breakeven"] + obs["shadow_time_stop"] + obs["shadow_timeout"]
        if n == 0:
            print(f"{setup_type:<30}{'-':>4}{'-':>4}{'-':>4}{'-':>4}{'-':>4}{'0':>5}{'no data':>9}")
            continue
        ts_n = obs["shadow_time_stop"] + obs["shadow_timeout"]
        print(f"{setup_type:<30}"
              f"{obs['shadow_tp']:>4}{obs['shadow_sl']:>4}{obs['shadow_breakeven']:>4}"
              f"{obs['shadow_time_stop']:>4}{obs['shadow_timeout']:>4}{n:>5}"
              f"{obs['shadow_tp']/n*100:>8.1f}%"
              f"{obs['shadow_sl']/n*100:>8.1f}%"
              f"{obs['avg_ts_pct']*100:>9.3f}%")
    print()

    print("PART 3 — Realistic breakeven WR with observed BE+TS distribution")
    print("-" * 96)
    print(f"{'signal':<30}{'p_be':>7}{'p_ts':>7}{'avg_TS%':>9}", end="")
    for label in FEE_MODELS:
        print(f"  {label.split()[0][:8]:>10}", end="")
    print(f"  {'observed':>10}")
    for setup_type, params in settings.SCALP_SIGNAL_PARAMS.items():
        obs = obs_data[setup_type]
        n = obs["shadow_tp"] + obs["shadow_sl"] + obs["shadow_breakeven"] + obs["shadow_time_stop"] + obs["shadow_timeout"]
        if n == 0:
            continue
        ts_n = obs["shadow_time_stop"] + obs["shadow_timeout"]
        p_be = obs["shadow_breakeven"] / n
        p_ts = ts_n / n
        avg_ts_pct = obs["avg_ts_pct"]
        observed_wr = obs["shadow_tp"] / n
        tp = params["tp_pct"] / 100.0
        sl = params["sl_pct"] / 100.0
        # Observed avg_ts_pct in DB already reflects fees (pnl_pct is net).
        # Add fee back to estimate gross.
        avg_ts_gross = avg_ts_pct + 0.0010  # back out current taker x2
        print(f"{setup_type:<30}{p_be:>6.2%}{p_ts:>6.2%}{avg_ts_gross*100:>8.3f}%", end="")
        for label, fee_rt in FEE_MODELS.items():
            wr = breakeven_wr_with_dist(tp, sl, fee_rt, p_be, p_ts, avg_ts_gross)
            verdict = ""
            if observed_wr >= wr:
                verdict = " OK"
            elif observed_wr >= wr * 0.7:
                verdict = " ?"
            else:
                verdict = " X"
            print(f"  {wr*100:>8.1f}%{verdict}", end="")
        print(f"  {observed_wr*100:>9.1f}%")
    print()
    print("Legend: OK = observed >= breakeven  ? = within 70% of breakeven  X = miss > 30%")

    print("\nPART 4 — Per-signal verdict")
    print("-" * 96)
    for setup_type, params in settings.SCALP_SIGNAL_PARAMS.items():
        obs = obs_data[setup_type]
        n = obs["shadow_tp"] + obs["shadow_sl"] + obs["shadow_breakeven"] + obs["shadow_time_stop"] + obs["shadow_timeout"]
        if n == 0:
            print(f"{setup_type:<30} NO DATA — fee viability is theoretical only")
            continue
        if n < 30:
            print(f"{setup_type:<30} STARVED (n={n} < 30) — observed WR not reliable")
            continue
        tp = params["tp_pct"] / 100.0
        sl = params["sl_pct"] / 100.0
        ts_n = obs["shadow_time_stop"] + obs["shadow_timeout"]
        p_be = obs["shadow_breakeven"] / n
        p_ts = ts_n / n
        avg_ts_gross = obs["avg_ts_pct"] + 0.0010
        observed_wr = obs["shadow_tp"] / n
        be_taker = breakeven_wr_with_dist(tp, sl, 0.0010, p_be, p_ts, avg_ts_gross)
        be_maker_maker = breakeven_wr_with_dist(tp, sl, 0.0004, p_be, p_ts, avg_ts_gross)
        gap_taker = observed_wr - be_taker
        gap_mm = observed_wr - be_maker_maker
        if observed_wr >= be_maker_maker:
            verdict = "VIABLE under maker+maker"
        elif gap_mm >= -0.05:
            verdict = "MARGINAL (within 5pp of maker+maker breakeven)"
        else:
            verdict = "KILL — observed WR below all fee models"
        print(f"{setup_type:<30} N={n:<4} obs_WR={observed_wr*100:.1f}% "
              f"be_taker={be_taker*100:.1f}% be_mk+mk={be_maker_maker*100:.1f}% "
              f"-> {verdict}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
