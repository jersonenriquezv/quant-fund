"""Manual trade auto-classifier.

Maps a context snapshot (from data_service.context_service) to:
  - auto_setup_type: which SMC setup best describes the trade
  - auto_confluences: list of objective confluences present at entry
  - auto_grade: A/B/C/D based on confluence count (decision quality, not outcome)

Deterministic, no ML. Runs inside the bybit_watcher on every position open.
Separate from the bot's live strategy detectors so manual classification
stays independent of runtime tuning.
"""
from __future__ import annotations

from typing import Any

CLASSIFIER_VERSION = 1


def _confluences(snap: dict[str, Any], direction: str) -> list[str]:
    """Collect objective confluences present in the snapshot."""
    out: list[str] = []

    htf = snap.get("htf_bias") or {}
    if htf.get("aligned_with_trade") is True:
        out.append("htf_4h_aligned")
    if htf.get("bias_1h") and htf.get("bias_1h") != "undefined":
        trade_bias = "bullish" if direction == "long" else "bearish"
        if htf.get("bias_1h") == trade_bias:
            out.append("htf_1h_aligned")

    smc = snap.get("smc") or {}
    obs = smc.get("obs_nearest") or {}
    for tf, ob in obs.items():
        if not isinstance(ob, dict):
            continue
        dist = abs(ob.get("distance_pct") or 999)
        if ob.get("in_zone"):
            out.append(f"OB_{tf}_in_zone")
        elif dist <= 1.0:
            out.append(f"OB_{tf}_near")

    fvgs = smc.get("fvgs_nearest") or {}
    for tf, fvg in fvgs.items():
        if not isinstance(fvg, dict):
            continue
        if fvg.get("in_zone"):
            out.append(f"FVG_{tf}_in_zone")
        elif abs(fvg.get("distance_pct") or 999) <= 1.0:
            out.append(f"FVG_{tf}_near")

    sweeps = smc.get("recent_sweeps") or []
    if sweeps:
        out.append("sweep_recent")
        # sweep with ≥3 touches = institutional
        if any((s.get("touch_count") or 0) >= 3 for s in sweeps):
            out.append("sweep_institutional")

    breaks = smc.get("recent_breaks") or []
    for brk in breaks:
        if brk.get("type") == "choch":
            out.append(f"CHoCH_{brk.get('tf')}")
        elif brk.get("type") == "bos":
            out.append(f"BOS_{brk.get('tf')}")
        if (brk.get("displacement_pct") or 0) >= 0.3:
            out.append("break_strong_displacement")

    cvd = snap.get("cvd") or {}
    cvd_1h = cvd.get("cvd_1h")
    if cvd_1h is not None:
        if direction == "long" and cvd_1h > 0:
            out.append("cvd_1h_aligned")
        elif direction == "short" and cvd_1h < 0:
            out.append("cvd_1h_aligned")

    f = snap.get("funding")
    if f is not None and abs(f) < 0.03:
        out.append("funding_neutral")

    oi1 = snap.get("oi_delta_1h_pct")
    if oi1 is not None and abs(oi1) < 2.0:
        out.append("oi_not_crowded")

    liq = snap.get("nearest_liq_cluster") or {}
    if liq and abs(liq.get("distance_pct") or 999) < 3.0:
        out.append("liq_cluster_magnet")

    vp = snap.get("volume_profile") or {}
    if vp.get("zone") == "inside_va":
        out.append("inside_value_area")
    near_hvn = vp.get("near_hvn") or {}
    if near_hvn and abs(near_hvn.get("distance_pct") or 999) < 0.5:
        out.append("at_hvn")

    ab = snap.get("absorption") or {}
    if ab.get("absorption_detected"):
        out.append("volume_absorption")
    if ab.get("displacement_detected"):
        out.append("volume_displacement")

    ob_book = snap.get("orderbook") or {}
    imb = ob_book.get("imbalance_top20")
    if imb is not None:
        if direction == "long" and imb > 0.15:
            out.append("orderbook_bid_heavy")
        elif direction == "short" and imb < -0.15:
            out.append("orderbook_ask_heavy")

    ml = snap.get("ml_features") or {}
    rsi_div = ml.get("rsi_divergence")
    if rsi_div and rsi_div != "none":
        trade_bias = "bullish" if direction == "long" else "bearish"
        if trade_bias in str(rsi_div):
            out.append(f"rsi_divergence_{rsi_div}")
    adx = ml.get("adx_14")
    adx_dir = ml.get("adx_direction")
    if adx is not None and adx >= 25:
        trade_bias = "bullish" if direction == "long" else "bearish"
        if adx_dir == trade_bias:
            out.append("adx_trending_aligned")
    stoch_cross = ml.get("stoch_rsi_cross")
    if stoch_cross:
        trade_bias = "bullish" if direction == "long" else "bearish"
        if stoch_cross == trade_bias:
            out.append(f"stoch_rsi_cross_{stoch_cross}")

    return out


def _detractors(snap: dict[str, Any], direction: str) -> list[str]:
    """Collect negative flags that penalize grade."""
    out: list[str] = []

    htf = snap.get("htf_bias") or {}
    if htf.get("aligned_with_trade") is False:
        out.append("counter_htf_4h")

    f = snap.get("funding")
    if f is not None:
        if direction == "long" and f > 0.05:
            out.append("funding_extreme_against_long")
        if direction == "short" and f < -0.05:
            out.append("funding_extreme_against_short")

    oi1 = snap.get("oi_delta_1h_pct")
    if oi1 is not None:
        if direction == "long" and oi1 > 3.0:
            out.append("oi_longs_crowded")
        if direction == "short" and oi1 > 3.0:
            out.append("oi_shorts_crowded")

    cvd = snap.get("cvd") or {}
    cvd_1h = cvd.get("cvd_1h")
    if cvd_1h is not None:
        if direction == "long" and cvd_1h < 0:
            out.append("cvd_1h_against")
        elif direction == "short" and cvd_1h > 0:
            out.append("cvd_1h_against")

    ml = snap.get("ml_features") or {}
    for flag in ml.get("momentum_flags") or []:
        out.append(f"ml_{flag}")

    vp = snap.get("volume_profile") or {}
    if vp.get("zone") == "above_va" and direction == "long":
        out.append("extended_above_vah")
    if vp.get("zone") == "below_va" and direction == "short":
        out.append("extended_below_val")

    return out


def _setup_type(snap: dict[str, Any], direction: str, confluences: list[str]) -> str:
    """Deterministic setup-type mapping from snapshot facts."""
    smc = snap.get("smc") or {}
    breaks = smc.get("recent_breaks") or []
    sweeps = smc.get("recent_sweeps") or []
    obs = smc.get("obs_nearest") or {}
    vp = snap.get("volume_profile") or {}

    has_choch = any(b.get("type") == "choch" for b in breaks)
    has_bos = any(b.get("type") == "bos" for b in breaks)
    has_sweep = len(sweeps) > 0
    ob_near_or_in = any(
        ob.get("in_zone") or abs(ob.get("distance_pct") or 999) <= 1.0
        for ob in obs.values() if isinstance(ob, dict)
    )

    htf_aligned = (snap.get("htf_bias") or {}).get("aligned_with_trade") is True

    # Priority order: sweep + reversal > trend continuation > reversal > breakout > discretion.
    if has_sweep and ob_near_or_in:
        return "B_sweep"
    if has_bos and htf_aligned and ob_near_or_in:
        return "A_swing_long" if direction == "long" else "A_swing_short"
    if has_choch and ob_near_or_in:
        return "D_choch"
    if has_bos and ob_near_or_in:
        return "D_bos"
    # Breakout — price outside VA with displacement
    if vp.get("zone") in ("above_va", "below_va"):
        ab = snap.get("absorption") or {}
        if ab.get("displacement_detected"):
            return "F_breakout"
    return "discretion"


def _grade(confluences: list[str], detractors: list[str]) -> str:
    """A/B/C/D based on net score."""
    score = len(confluences) - len(detractors)
    if score >= 6:
        return "A"
    if score >= 4:
        return "B"
    if score >= 2:
        return "C"
    return "D"


def classify(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Classify a snapshot built by context_service.build_context_snapshot.

    Returns dict with keys: auto_setup_type, auto_confluences,
    auto_detractors, auto_grade, auto_classifier_version.
    """
    direction = snapshot.get("direction") or "long"
    conflu = _confluences(snapshot, direction)
    detr = _detractors(snapshot, direction)
    setup = _setup_type(snapshot, direction, conflu)
    grade = _grade(conflu, detr)
    return {
        "auto_setup_type": setup,
        "auto_confluences": conflu,
        "auto_detractors": detr,
        "auto_grade": grade,
        "auto_classifier_version": CLASSIFIER_VERSION,
    }
