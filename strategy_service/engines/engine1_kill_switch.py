"""Engine 1 live-gate kill switch — pure metrics over closed-trade PnL.

Phase 2 of docs/plans/engine1-ml-filter-live.md. Computes the three
data-driven kill conditions from grill Q6 over the realized PnL of engine1
LIVE trades, in chronological (oldest-first) order:

  - cumulative drawdown (peak-to-trough equity, in R units) > dd_r_limit
  - >= consec_limit consecutive losing trades (trailing run)
  - rolling-window profit factor < pf_floor once >= pf_window trades exist

Pure: no I/O, no settings import. The caller (main.py) fetches closed
engine1 trades, passes their `pnl_usd` list + `ENGINE1_RISK_USD` + thresholds,
and reverts new live entries to shadow + alerts when `triggered` is True.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KillVerdict:
    triggered: bool
    reason: str | None
    n_trades: int
    max_dd_r: float
    consec_losses: int
    rolling_pf: float | None  # None until >= pf_window trades


def _max_drawdown_r(pnls: list[float], r_usd: float) -> float:
    """Max peak-to-trough drawdown of the cumulative equity curve, in R units.

    R-normalized so the threshold is exchange/capital independent. Returns a
    non-negative magnitude (0.0 = monotonic-up curve or empty input).
    """
    if r_usd <= 0:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd / r_usd


def _trailing_consecutive_losses(pnls: list[float]) -> int:
    """Count losing trades (pnl < 0) at the tail of the sequence."""
    count = 0
    for pnl in reversed(pnls):
        if pnl < 0:
            count += 1
        else:
            break
    return count


def _rolling_profit_factor(pnls: list[float], window: int) -> float | None:
    """Profit factor over the last `window` trades.

    Returns None if fewer than `window` trades exist (not enough data to judge).
    PF = sum(wins) / abs(sum(losses)). All-wins window -> inf.
    """
    if window <= 0 or len(pnls) < window:
        return None
    recent = pnls[-window:]
    gains = sum(p for p in recent if p > 0)
    losses = -sum(p for p in recent if p < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def evaluate_kill(
    pnls: list[float],
    *,
    r_usd: float,
    dd_r_limit: float,
    consec_limit: int,
    pf_floor: float,
    pf_window: int,
) -> KillVerdict:
    """Evaluate all three kill conditions. First breach (in listed order) wins.

    `pnls` must be chronological (oldest first). Empty -> no trigger.
    """
    n = len(pnls)
    max_dd_r = _max_drawdown_r(pnls, r_usd)
    consec = _trailing_consecutive_losses(pnls)
    rolling_pf = _rolling_profit_factor(pnls, pf_window)

    reason: str | None = None
    if max_dd_r > dd_r_limit:
        reason = (
            f"cumulative drawdown {max_dd_r:.1f}R > {dd_r_limit:.0f}R limit "
            f"(N={n})"
        )
    elif consec >= consec_limit:
        reason = f"{consec} consecutive losses >= {consec_limit} limit (N={n})"
    elif rolling_pf is not None and rolling_pf < pf_floor:
        reason = (
            f"rolling-{pf_window} PF {rolling_pf:.2f} < {pf_floor:.2f} floor "
            f"(N={n})"
        )

    return KillVerdict(
        triggered=reason is not None,
        reason=reason,
        n_trades=n,
        max_dd_r=max_dd_r,
        consec_losses=consec,
        rolling_pf=rolling_pf,
    )
