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

CLASSIFIER_VERSION = 2


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


def _map_bias(b: str | None) -> str | None:
    """Snapshot bias (bullish/bearish/undefined) -> v2 taxonomy (bullish/bearish/range)."""
    if b in ("bullish", "bearish"):
        return b
    if b == "undefined":
        return "range"
    return None


def _v2_chain(snap: dict[str, Any], direction: str, detractors: list[str]) -> dict[str, Any]:
    """Emit the journal v2 top-down chain pre-fill from the context snapshot.

    Closed-vocab best-effort guess that pre-fills the form; the human confirms or
    corrects. A human/machine disagreement IS the misread signal — these auto_*
    values are stored alongside (never instead of) the human columns. See
    docs/plans/bybit-journal-v2-2026-05-30.md Phase 3.
    """
    out: dict[str, Any] = {}
    htf = snap.get("htf_bias") or {}
    trade_bias = "bullish" if direction == "long" else "bearish"

    bias_daily = _map_bias(htf.get("bias_daily"))
    bias_4h = _map_bias(htf.get("bias_4h"))
    bias_1h_raw = htf.get("bias_1h")
    aligned_4h = htf.get("aligned_with_trade") is True

    out["auto_htf_bias_daily"] = bias_daily
    out["auto_htf_bias_4h"] = bias_4h
    out["auto_htf_structure_reason"] = (
        "HH_HL" if bias_4h == "bullish"
        else "LH_LL" if bias_4h == "bearish"
        else "range_bound" if bias_4h == "range"
        else "unclear"
    )

    # 1H multi-timeframe alignment vs trade direction.
    if bias_1h_raw == trade_bias:
        out["auto_mtf_1h"] = "confirms"
    elif bias_1h_raw in ("bullish", "bearish"):
        out["auto_mtf_1h"] = "contradicts"
    else:
        out["auto_mtf_1h"] = "neutral"

    # Premium/discount proxy from volume-profile zone (rough — user corrects).
    vp = snap.get("volume_profile") or {}
    pd = {"above_va": "premium", "inside_va": "equilibrium", "below_va": "discount"}.get(vp.get("zone"))
    out["auto_location_pd"] = pd

    # Location quality: at a meaningful level (OB / FVG / sweep / HVN) vs no-man's-land.
    smc = snap.get("smc") or {}
    obs = smc.get("obs_nearest") or {}
    fvgs = smc.get("fvgs_nearest") or {}
    sweeps = smc.get("recent_sweeps") or []
    breaks = smc.get("recent_breaks") or []
    at_ob = any(
        isinstance(o, dict) and (o.get("in_zone") or abs(o.get("distance_pct") or 999) <= 0.5)
        for o in obs.values()
    )
    at_fvg = any(
        isinstance(f, dict) and (f.get("in_zone") or abs(f.get("distance_pct") or 999) <= 0.5)
        for f in fvgs.values()
    )
    near_hvn = vp.get("near_hvn") or {}
    at_hvn = bool(near_hvn) and abs(near_hvn.get("distance_pct") or 999) < 0.5
    at_key = at_ob or at_fvg or bool(sweeps) or at_hvn
    out["auto_location_quality"] = "key_level" if at_key else "no_mans_land"

    # LTF trigger precedence: sweep_reclaim > choch > bos > fvg > order_block.
    has_choch = any(b.get("type") == "choch" for b in breaks)
    has_bos = any(b.get("type") == "bos" for b in breaks)
    if sweeps:
        trigger = "sweep_reclaim"
    elif has_choch:
        trigger = "choch"
    elif has_bos:
        trigger = "bos"
    elif at_fvg:
        trigger = "fvg"
    elif at_ob:
        trigger = "order_block"
    else:
        trigger = None
    out["auto_ltf_trigger"] = trigger

    # Structure type: range > reversal (choch / counter-trend sweep) > continuation.
    if bias_4h == "range":
        structure = "range"
    elif has_choch or (sweeps and not aligned_4h):
        structure = "reversal"
    else:
        structure = "continuation"
    out["auto_structure_type"] = structure

    # 5 independent confluence factors (HTF + trigger mandatory; range branch swaps
    # HTF-dir for sweep_reclaim + location per locked decision).
    pd_ok = (
        pd is None
        or (direction == "long" and pd in ("discount", "equilibrium"))
        or (direction == "short" and pd in ("premium", "equilibrium"))
    )
    detr = set(detractors)
    out["auto_conf_htf"] = aligned_4h
    out["auto_conf_location"] = at_key and pd_ok
    out["auto_conf_mtf"] = out["auto_mtf_1h"] == "confirms"
    out["auto_conf_trigger"] = trigger is not None
    out["auto_conf_noconflict"] = not (
        "cvd_1h_against" in detr
        or "funding_extreme_against_long" in detr
        or "funding_extreme_against_short" in detr
    )
    return out


def classify(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Classify a snapshot built by context_service.build_context_snapshot.

    Returns dict with keys: auto_setup_type, auto_confluences, auto_detractors,
    auto_grade, auto_classifier_version, plus the journal v2 top-down chain
    pre-fill (auto_htf_bias_daily, auto_htf_bias_4h, auto_htf_structure_reason,
    auto_location_pd, auto_location_quality, auto_mtf_1h, auto_ltf_trigger,
    auto_structure_type, and the 5 auto_conf_* booleans).
    """
    direction = snapshot.get("direction") or "long"
    conflu = _confluences(snapshot, direction)
    detr = _detractors(snapshot, direction)
    setup = _setup_type(snapshot, direction, conflu)
    grade = _grade(conflu, detr)
    result = {
        "auto_setup_type": setup,
        "auto_confluences": conflu,
        "auto_detractors": detr,
        "auto_grade": grade,
        "auto_classifier_version": CLASSIFIER_VERSION,
    }
    result.update(_v2_chain(snapshot, direction, detr))
    return result
