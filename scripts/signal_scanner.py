"""Daily signal scanner — Telegram alerts for high-grade auto-classifier setups.

Iterates TRADING_PAIRS × {long, short}. For each pair/direction:
  1. Builds a context snapshot via data_service.context_service.
  2. Classifies via strategy_service.trade_classifier.
  3. If grade is A or B, computes entry / SL / TP and emits a Telegram alert.

The scanner does NOT execute trades. It is a heads-up signal — the user
inspects, sizes, and places the order manually on Bybit. Live execution
stays gated behind ENABLED_SETUPS as documented in docs/SYSTEM_BASELINE.md.

Run:  python scripts/signal_scanner.py
Cron: docs/systemd/signal-scanner.timer (hourly).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor

from config.settings import settings
from data_service.context_service import build_context_snapshot
from shared.logger import setup_logger
from shared.notifier import TelegramNotifier

logger = setup_logger("signal_scanner")


MIN_GRADE = "B"  # alert on A and B
GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}
MIN_RR = 1.5
ATR_SL_MULT = 1.2     # SL = OB extreme or current price - 1.2 * ATR_1h
TP_RR_TARGET = 2.0    # TP placed at 2R initially; structural fallback if available
DEDUP_HOURS = 6       # don't re-alert the same pair/direction within this window

# --- Edge engine (topdown triplet) -----------------------------------------
# The classifier path above (grade A/B) has no out-of-sample edge. The edge
# engine swaps in the /topdown triplet logic, which measured +0.20R maker
# (deduped) on BTC/ETH. See docs/plans/signal-scanner-topdown-edge-2026-05-25.md.
SCANNER_PAIRS = ["BTC/USDT", "ETH/USDT"]  # edge confirmed only here, not TRADING_PAIRS
MAX_SWEEP_PCT = 0.5   # actionable sweep gate — tighter than topdown's 1.0% spectator cap


def _pair_to_bybit(pair: str) -> str:
    """ETH/USDT → ETHUSDT."""
    return pair.replace("/", "")


def _ensure_alert_table() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS signal_scanner_alerts (
        id BIGSERIAL PRIMARY KEY,
        scanned_at TIMESTAMPTZ DEFAULT NOW(),
        pair VARCHAR(20) NOT NULL,
        direction VARCHAR(5) NOT NULL,
        auto_setup_type VARCHAR(40),
        auto_grade VARCHAR(2),
        net_score INT,
        entry DOUBLE PRECISION,
        sl DOUBLE PRECISION,
        tp DOUBLE PRECISION,
        rr DOUBLE PRECISION,
        confluences JSONB,
        detractors JSONB,
        snapshot JSONB
    );
    CREATE INDEX IF NOT EXISTS idx_signal_scanner_pair_dir_time
        ON signal_scanner_alerts(pair, direction, scanned_at DESC);
    -- Edge-engine geometry promoted out of snapshot JSONB for WR reconciliation
    -- against Bybit closes (idempotent; pre-edge rows leave these NULL).
    ALTER TABLE signal_scanner_alerts
        ADD COLUMN IF NOT EXISTS sweep_distance_pct DOUBLE PRECISION;
    ALTER TABLE signal_scanner_alerts
        ADD COLUMN IF NOT EXISTS risk_pct DOUBLE PRECISION;
    ALTER TABLE signal_scanner_alerts
        ADD COLUMN IF NOT EXISTS bias_confidence VARCHAR(10);
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql)
        c.commit()


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _recently_alerted(pair: str, direction: str) -> bool:
    """True if the same pair/direction was alerted in the last DEDUP_HOURS."""
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT scanned_at FROM signal_scanner_alerts
            WHERE pair = %s AND direction = %s
              AND scanned_at >= NOW() - (%s * INTERVAL '1 hour')
            ORDER BY scanned_at DESC LIMIT 1
            """,
            (pair, direction, DEDUP_HOURS),
        )
        return cur.fetchone() is not None


def _record_alert(
    pair: str,
    direction: str,
    setup_type: str,
    grade: str,
    net_score: int,
    geom: dict[str, float],
    confluences: list[str],
    detractors: list[str],
    snap: dict[str, Any],
) -> None:
    import json
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_scanner_alerts
                (pair, direction, auto_setup_type, auto_grade, net_score,
                 entry, sl, tp, rr, confluences, detractors, snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                pair, direction, setup_type, grade, net_score,
                geom["entry"], geom["sl"], geom["tp"], geom["rr"],
                json.dumps(confluences), json.dumps(detractors),
                json.dumps(snap, default=str),
            ),
        )
        c.commit()


@dataclass
class Geometry:
    entry: float
    sl: float
    tp: float
    rr: float
    sl_source: str
    tp_source: str


def _compute_geometry(snap: dict[str, Any], direction: str) -> Geometry | None:
    """Compute entry/SL/TP from context snapshot.

    Entry: current price.
    SL:    nearest aligned OB extreme (1H/4H preferred), else 1.2 × ATR(1h) buffer.
    TP:    first structural level (POC/VAH/VAL/HVN) at >= MIN_RR; else 2R fallback.
    """
    price = snap.get("current_price")
    if not price:
        return None

    smc = snap.get("smc") or {}
    obs = smc.get("obs_nearest") or {}
    liq = snap.get("nearest_liq_cluster") or {}
    atr = liq.get("atr_1h") if isinstance(liq, dict) else None

    sl: float | None = None
    sl_source = ""

    # Prefer 1H, then 4H OB extreme as SL anchor
    for tf in ("1h", "4h"):
        ob = obs.get(tf)
        if not isinstance(ob, dict):
            continue
        if direction == "long":
            anchor = ob.get("low")
            if anchor is None or anchor >= price:
                continue
            buf = (atr * 0.5) if atr else (price * 0.001)
            sl = float(anchor) - buf
        else:
            anchor = ob.get("high")
            if anchor is None or anchor <= price:
                continue
            buf = (atr * 0.5) if atr else (price * 0.001)
            sl = float(anchor) + buf
        sl_source = f"OB_{tf}"
        break

    # ATR fallback if no usable OB
    if sl is None and atr:
        if direction == "long":
            sl = price - ATR_SL_MULT * atr
        else:
            sl = price + ATR_SL_MULT * atr
        sl_source = "ATR_1h"

    if sl is None:
        return None

    risk = abs(price - sl)
    if risk <= 0:
        return None
    # Hard floor on SL distance — match bot's MIN_RISK_DISTANCE_PCT
    min_dist = price * settings.MIN_RISK_DISTANCE_PCT
    if risk < min_dist:
        if direction == "long":
            sl = price - min_dist
        else:
            sl = price + min_dist
        risk = min_dist
        sl_source += "+floor"

    # TP — try volume profile structural levels first
    vp = snap.get("volume_profile") or {}
    candidates: list[tuple[float, str]] = []
    for key, label in (("poc", "POC"), ("vah", "VAH"), ("val", "VAL")):
        v = vp.get(key)
        if v is None:
            continue
        if direction == "long" and v > price:
            candidates.append((float(v), label))
        if direction == "short" and v < price:
            candidates.append((float(v), label))
    near_hvn = vp.get("near_hvn") or {}
    if isinstance(near_hvn, dict) and near_hvn.get("price") is not None:
        hvn_p = float(near_hvn["price"])
        if direction == "long" and hvn_p > price:
            candidates.append((hvn_p, "HVN"))
        if direction == "short" and hvn_p < price:
            candidates.append((hvn_p, "HVN"))

    candidates.sort(key=lambda c: abs(c[0] - price))
    tp: float | None = None
    tp_source = ""
    for cand_price, label in candidates:
        cand_rr = abs(cand_price - price) / risk
        if cand_rr >= MIN_RR:
            tp = cand_price
            tp_source = label
            break

    if tp is None:
        # 2R fallback
        tp = price + TP_RR_TARGET * risk if direction == "long" else price - TP_RR_TARGET * risk
        tp_source = "2R"

    rr = abs(tp - price) / risk
    return Geometry(entry=price, sl=sl, tp=tp, rr=rr, sl_source=sl_source, tp_source=tp_source)


def _format_telegram(
    pair: str,
    direction: str,
    classification: dict[str, Any],
    geom: Geometry,
) -> str:
    arrow = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    grade = classification.get("auto_grade", "?")
    setup = classification.get("auto_setup_type", "?")
    conflu = classification.get("auto_confluences") or []
    detr = classification.get("auto_detractors") or []
    net = len(conflu) - len(detr)

    # Top 3 confluences for brevity
    conflu_short = ", ".join(conflu[:3])
    detr_short = ", ".join(detr[:2]) if detr else "none"

    return (
        f"📡 <b>SIGNAL · {pair} · {arrow}</b>\n"
        f"<b>Grade {grade}</b> · {setup} · net {'+' if net >= 0 else ''}{net}\n"
        f"\n"
        f"<b>ENTRY</b> {geom.entry:.4f}\n"
        f"<b>SL</b>    {geom.sl:.4f}  <i>({geom.sl_source})</i>\n"
        f"<b>TP</b>    {geom.tp:.4f}  <i>({geom.tp_source})</i>\n"
        f"<b>R:R</b>   {geom.rr:.2f}\n"
        f"\n"
        f"+ {conflu_short}\n"
        f"− {detr_short}\n"
        f"\n"
        f"<i>Auto-classifier only. Not executed. Verify on chart before placing on Bybit.</i>"
    )


async def scan_classifier(dry_run: bool = False) -> list[dict[str, Any]]:
    """DEAD PATH (retained for replay) — the original classifier engine.

    Scanned TRADING_PAIRS × {long, short}, alerted on grade A/B. Proven to
    carry no out-of-sample edge; replaced by the edge engine in `scan()` on
    2026-05-26 (docs/plans/signal-scanner-topdown-edge-2026-05-25.md). Not on
    any live path. `classify` imported locally so the live path stays clean.
    """
    from strategy_service.trade_classifier import classify

    _ensure_alert_table()
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)

    pairs = settings.TRADING_PAIRS
    out: list[dict[str, Any]] = []
    for pair in pairs:
        bybit_sym = _pair_to_bybit(pair)
        for direction in ("long", "short"):
            try:
                side = "Buy" if direction == "long" else "Sell"
                snap = build_context_snapshot(bybit_sym, side)
                if snap.get("error"):
                    logger.warning(f"{pair} {direction}: snapshot error: {snap['error']}")
                    continue

                classification = classify(snap)
                grade = classification.get("auto_grade")
                if not grade or GRADE_RANK.get(grade, 0) < GRADE_RANK[MIN_GRADE]:
                    continue

                geom = _compute_geometry(snap, direction)
                if not geom:
                    logger.info(f"{pair} {direction}: grade {grade} but no geometry")
                    continue
                if geom.rr < MIN_RR:
                    logger.info(f"{pair} {direction}: grade {grade} R:R {geom.rr:.2f} < {MIN_RR}")
                    continue

                if not dry_run and _recently_alerted(pair, direction):
                    logger.info(f"{pair} {direction}: dedup — alerted within {DEDUP_HOURS}h")
                    continue

                msg = _format_telegram(pair, direction, classification, geom)
                logger.info(
                    f"ALERT {pair} {direction} grade={grade} setup={classification['auto_setup_type']} "
                    f"entry={geom.entry:.4f} sl={geom.sl:.4f} tp={geom.tp:.4f} rr={geom.rr:.2f}"
                )
                if dry_run:
                    print(msg)
                    print("---")
                else:
                    await notifier.send(msg)
                    _record_alert(
                        pair, direction,
                        classification["auto_setup_type"],
                        grade,
                        len(classification["auto_confluences"]) - len(classification["auto_detractors"]),
                        {"entry": geom.entry, "sl": geom.sl, "tp": geom.tp, "rr": geom.rr},
                        classification["auto_confluences"],
                        classification["auto_detractors"],
                        snap,
                    )
                out.append({
                    "pair": pair,
                    "direction": direction,
                    "grade": grade,
                    "setup": classification["auto_setup_type"],
                    "entry": geom.entry,
                    "sl": geom.sl,
                    "tp": geom.tp,
                    "rr": geom.rr,
                })
            except Exception as exc:
                logger.error(f"{pair} {direction}: scan failed: {exc}", exc_info=True)
    return out


def _edge_candidate(pair: str, signal: dict[str, Any]) -> dict[str, Any] | None:
    """Apply the scanner gate to a raw edge signal. Returns a normalized
    candidate dict or None when the signal fails the gate.

    Gate: sweep ≤ MAX_SWEEP_PCT, geometry SL on the protective side
    (long sl<entry, short sl>entry), rr>0, single TP = triplet final target.
    """
    side = signal["side"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    rr = signal["rr"]
    sweep_pct = signal["sweep_distance_pct"]

    if sweep_pct is None or sweep_pct > MAX_SWEEP_PCT:
        return None
    if side == "long" and not sl < entry:
        return None
    if side == "short" and not sl > entry:
        return None
    if rr is None or rr <= 0:
        return None

    return {
        "pair": pair,
        "direction": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,                      # single TP — triplet final target
        "rr": rr,
        "sweep_distance_pct": sweep_pct,
        "risk_pct": signal.get("risk_pct"),
        "bias_confidence": signal.get("bias_confidence"),
        "current_price": signal.get("current_price"),
    }


def _format_telegram_edge(cand: dict[str, Any]) -> str:
    """Mobile-friendly LIMIT alert for an edge candidate.

    Leads with the limit price (maker entry), then SL, single TP, R:R, bias
    confidence, and sweep distance. States "orden límite" explicitly so the
    user never market-fills. Short lines only — no wide monospace columns.
    """
    direction = cand["direction"]
    arrow = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    bias = cand.get("bias_confidence") or "?"
    risk_pct = cand.get("risk_pct")
    risk_str = f"{risk_pct:.2f}%" if isinstance(risk_pct, (int, float)) else "?"
    return (
        f"📡 <b>EDGE · {cand['pair']} · {arrow}</b>\n"
        f"<i>orden límite (maker)</i>\n"
        f"\n"
        f"<b>LIMIT {direction.upper()} @ {cand['entry']:.6g}</b>\n"
        f"<b>SL</b>  {cand['sl']:.6g}\n"
        f"<b>TP</b>  {cand['tp']:.6g}\n"
        f"<b>R:R</b> {cand['rr']:.2f}\n"
        f"\n"
        f"bias {bias} · sweep {cand['sweep_distance_pct']:.2f}% · risk {risk_str}\n"
        f"\n"
        f"<i>Edge engine (topdown triplet). Not executed. "
        f"Coloca una orden límite en Bybit.</i>"
    )


def _record_alert_edge(cand: dict[str, Any]) -> None:
    """Persist an edge alert, tagged auto_setup_type='topdown_edge'.

    Stores entry/sl/tp/rr in their columns; sweep/risk/bias/price go in the
    snapshot JSONB until Phase 3 promotes them to dedicated columns.
    """
    import json
    snap = {
        "sweep_distance_pct": cand.get("sweep_distance_pct"),
        "risk_pct": cand.get("risk_pct"),
        "bias_confidence": cand.get("bias_confidence"),
        "current_price": cand.get("current_price"),
    }
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_scanner_alerts
                (pair, direction, auto_setup_type, auto_grade, net_score,
                 entry, sl, tp, rr, sweep_distance_pct, risk_pct,
                 bias_confidence, confluences, detractors, snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                cand["pair"], cand["direction"], "topdown_edge", None, None,
                cand["entry"], cand["sl"], cand["tp"], cand["rr"],
                cand.get("sweep_distance_pct"), cand.get("risk_pct"),
                cand.get("bias_confidence"),
                None, None, json.dumps(snap, default=str),
            ),
        )
        c.commit()


async def scan(dry_run: bool = False) -> list[dict[str, Any]]:
    """Edge engine — the live scanner path.

    Iterates SCANNER_PAIRS (BTC/ETH), builds the /topdown triplet via
    `build_edge_signal`, applies the scanner gate (`_edge_candidate`), dedups
    6h per pair+direction, and emits a LIMIT (maker) alert. Returns the
    candidates produced. The classifier engine is retained dead in
    `scan_classifier()` for replay only.
    """
    from scripts.topdown_snapshot import build_edge_signal

    _ensure_alert_table()
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)

    out: list[dict[str, Any]] = []
    for pair in SCANNER_PAIRS:
        try:
            signal = build_edge_signal(pair)
            if signal is None:
                logger.info(f"{pair}: no valid edge triplet")
                continue
            cand = _edge_candidate(pair, signal)
            if cand is None:
                logger.info(
                    f"{pair}: triplet rejected by gate "
                    f"(sweep={signal['sweep_distance_pct']}, side={signal['side']})"
                )
                continue

            direction = cand["direction"]
            if not dry_run and _recently_alerted(pair, direction):
                logger.info(f"{pair} {direction}: dedup — alerted within {DEDUP_HOURS}h")
                continue

            msg = _format_telegram_edge(cand)
            logger.info(
                f"EDGE {pair} {direction} entry={cand['entry']:.6g} "
                f"sl={cand['sl']:.6g} tp={cand['tp']:.6g} rr={cand['rr']:.2f} "
                f"sweep={cand['sweep_distance_pct']:.2f}%"
            )
            if dry_run:
                print(msg)
                print("---")
            else:
                await notifier.send(msg)
                _record_alert_edge(cand)
            out.append(cand)
        except Exception as exc:
            logger.error(f"{pair}: edge scan failed: {exc}", exc_info=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily signal scanner for manual Bybit trades.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print to stdout instead of sending Telegram + writing DB.")
    args = ap.parse_args()

    started = time.time()
    alerts = asyncio.run(scan(dry_run=args.dry_run))
    elapsed = time.time() - started
    logger.info(f"scan complete: {len(alerts)} alerts in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
