"""Tests for the engine1 live-gate kill switch (pure metrics, no model dep).

Covers the three grill-Q6 kill conditions over realized engine1 PnL:
drawdown-in-R, consecutive losses, rolling-window profit factor.
"""
from strategy_service.engines import engine1_kill_switch as ks

R = 1.5  # ENGINE1_RISK_USD default

# Default thresholds mirror config/settings.py.
_KW = dict(r_usd=R, dd_r_limit=10.0, consec_limit=7, pf_floor=1.2, pf_window=20)


def _verdict(pnls, **over):
    kw = {**_KW, **over}
    return ks.evaluate_kill(pnls, **kw)


# --- empty / benign ---

def test_empty_history_never_triggers():
    v = _verdict([])
    assert v.triggered is False
    assert v.n_trades == 0
    assert v.rolling_pf is None


def test_all_wins_no_trigger():
    v = _verdict([R] * 25)
    assert v.triggered is False
    assert v.max_dd_r == 0.0
    assert v.consec_losses == 0


# --- drawdown in R ---

def test_drawdown_in_r_triggers_over_limit():
    # 11 straight losses of 1R = 11R peak-to-trough drawdown > 10R limit.
    v = _verdict([-R] * 11)
    assert v.triggered is True
    assert "drawdown" in v.reason
    assert v.max_dd_r == 11.0


def test_drawdown_exactly_at_limit_does_not_trigger():
    # 10R == limit; strictly-greater required.
    v = _verdict([-R] * 10, consec_limit=99)
    assert v.max_dd_r == 10.0
    assert v.triggered is False


def test_drawdown_uses_peak_to_trough_not_final():
    # Up 5R, then down 12R: trough is 12R below the peak even though we recover.
    pnls = [R] * 5 + [-R] * 12 + [R] * 3
    v = _verdict(pnls, consec_limit=99, pf_window=999)
    assert v.max_dd_r == 12.0
    assert v.triggered is True


# --- consecutive losses ---

def test_consecutive_losses_trigger():
    # 7 trailing losses, small enough not to breach DD (limit raised).
    pnls = [R, R, R] + [-0.2 * R] * 7
    v = _verdict(pnls, dd_r_limit=999)
    assert v.consec_losses == 7
    assert v.triggered is True
    assert "consecutive" in v.reason


def test_a_win_resets_the_loss_streak():
    pnls = [-R] * 6 + [R] + [-R] * 3  # trailing run is only 3
    v = _verdict(pnls, dd_r_limit=999, pf_window=999)
    assert v.consec_losses == 3
    assert v.triggered is False


# --- rolling profit factor ---

def test_rolling_pf_none_below_window():
    v = _verdict([R, -R, R])
    assert v.rolling_pf is None
    assert v.triggered is False


def test_rolling_pf_below_floor_triggers():
    # 20 trades: 8 wins of 1R, 12 losses of 1R -> PF = 8/12 = 0.67 < 1.2.
    pnls = [R] * 8 + [-R] * 12
    # Reorder so the tail isn't 12 straight losses (avoid consec/DD firing).
    pnls = [R, -R] * 8 + [-R] * 4
    v = _verdict(pnls, dd_r_limit=999, consec_limit=99)
    assert v.rolling_pf is not None and v.rolling_pf < 1.2
    assert v.triggered is True
    assert "PF" in v.reason


def test_rolling_pf_above_floor_no_trigger():
    # 20 trades alternating +2R / -1R -> PF = 20/10 = 2.0 >= 1.2.
    pnls = [2 * R, -R] * 10
    v = _verdict(pnls, dd_r_limit=999, consec_limit=99)
    assert v.rolling_pf == 2.0
    assert v.triggered is False


def test_rolling_pf_all_wins_is_inf_not_trigger():
    v = _verdict([R] * 20, dd_r_limit=999, consec_limit=99)
    assert v.rolling_pf == float("inf")
    assert v.triggered is False


# --- ordering: DD checked before consec before PF ---

def test_drawdown_reason_wins_when_multiple_breach():
    # 11 straight losses breach DD (11R), consec (11), and PF — DD reported.
    v = _verdict([-R] * 11)
    assert "drawdown" in v.reason


def test_zero_r_usd_safe():
    # Defensive: r_usd<=0 -> dd normalizes to 0, no divide-by-zero.
    v = _verdict([-1.0] * 11, r_usd=0.0)
    assert v.max_dd_r == 0.0
