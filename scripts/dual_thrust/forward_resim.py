"""Engine 2 / Dual Thrust ETH 6h — FORWARD paper re-simulation (Option 1).

Faithful forward validation WITHOUT touching the bot pipeline. Dual Thrust is
stop-and-reverse with no TP (68% of backtest exits are flips), which the bot's
fixed TP/SL/timeout ShadowMonitor cannot model. Instead we re-run the SAME
validated pandas strategy (flip + ATR SL, funding-adjusted) on freshly fetched
OKX candles and slice out the trades opened AFTER the freeze date. Those are
genuine out-of-sample forward trades, params frozen — the honest test of whether
the Sharpe-2.0 edge survives going forward.

Run weekly (cron). Deterministic: each run re-fetches fresh candles and re-slices,
so it is idempotent — the forward CSV is rebuilt, not blindly appended.

  bot-venv: ~/quant-fund/venv/bin/python ~/jesse-research/project/forward_resim.py

Frozen params = the Phase-1/2 winner (okx_revalidation.HP). Freeze date below.
Decision (Phase 4): at N>=25 forward trades OR 180 days, gate = forward PF>=1.3
AND forward net>0. Interim runs just monitor the trend.
"""
from __future__ import annotations

import os
import json
import datetime as dt

import numpy as np
import pandas as pd

import okx_revalidation as M

# Freeze: params fixed as of this date; trades opened on/after it are forward.
FREEZE_DATE = "2026-06-13"
DECISION_MIN_TRADES = 25
DECISION_MAX_DAYS = 180
DECISION_PF_BAR = 1.3

FWD_DIR = os.path.join(os.path.dirname(__file__), "forward")
FWD_TRADES_CSV = os.path.join(FWD_DIR, "dual_thrust_eth_forward_trades.csv")
FWD_RUNLOG_CSV = os.path.join(FWD_DIR, "dual_thrust_eth_runlog.csv")


def _load_bot_env(keys):
    """Read selected keys from the bot's config/.env (Telegram creds)."""
    path = os.path.expanduser("~/quant-fund/config/.env")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                if k.strip() in keys:
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _telegram(msg: str):
    """Best-effort Telegram push using the bot's credentials."""
    try:
        import requests
        env = _load_bot_env({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"})
        token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def _prev_forward_count():
    """Forward trade count from the last runlog row (None if no prior run)."""
    if not os.path.exists(FWD_RUNLOG_CSV):
        return None
    try:
        prev = pd.read_csv(FWD_RUNLOG_CSV)
        if len(prev) and "trades" in prev.columns:
            return int(prev["trades"].iloc[-1])
    except Exception:
        pass
    return None


def _pf(pnls: np.ndarray) -> float:
    gains = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    return float(gains / losses) if losses > 0 else float("inf")


def _trade_stats(tdf: pd.DataFrame) -> dict:
    if len(tdf) == 0:
        return {"trades": 0}
    pnl = tdf["pnl_net"].to_numpy()
    wins = int((pnl > 0).sum())
    hold_h = (tdf["exit_ts"] - tdf["entry_ts"]) / 3_600_000
    return {
        "trades": len(tdf),
        "win_rate": round(wins / len(tdf), 4),
        "pf": round(_pf(pnl), 3),
        "net_pnl_usd": round(float(pnl.sum()), 2),
        "expectancy_usd": round(float(pnl.mean()), 2),
        "avg_ret_pct": round(float(tdf["ret"].mean() * 100), 3),
        "flips": int((tdf["reason"] == "flip").sum()),
        "sl_exits": int((tdf["reason"] == "sl").sum()),
        "median_hold_h": round(float(hold_h.median()), 1),
    }


def run():
    os.makedirs(FWD_DIR, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    freeze_ms = pd.Timestamp(FREEZE_DATE, tz="UTC").value // 1_000_000

    # --- fresh candles (refresh cache) + funding ---
    cache = os.path.join(M.CACHE_DIR, "ETH-USDT-SWAP_6h.csv")
    if os.path.exists(cache):
        os.remove(cache)  # force re-fetch so forward window stays current
    okx6 = M.load_okx_6h("ETH-USDT-SWAP", 3100)
    okx1d = M._resample_ohlc(okx6, "1D")
    fcache = os.path.join(M.CACHE_DIR, "ETH-USDT-SWAP_funding.csv")
    if os.path.exists(fcache):
        os.remove(fcache)
    fdf = M.load_okx_funding("ETH-USDT-SWAP", 2400)
    funding = (fdf["timestamp"].to_numpy(), fdf["rate"].to_numpy())

    # --- full-history faithful backtest (warmup handled internally) ---
    _, trades, _ = M.backtest(okx6, okx1d, funding=funding)
    tdf = pd.DataFrame(trades)
    tdf["entry_dt"] = pd.to_datetime(tdf["entry_ts"], unit="ms", utc=True)
    tdf["exit_dt"] = pd.to_datetime(tdf["exit_ts"], unit="ms", utc=True)

    insample = tdf[tdf["entry_ts"] < freeze_ms]
    forward = tdf[tdf["entry_ts"] >= freeze_ms].reset_index(drop=True)

    # --- persist forward trades (idempotent rebuild) + run log ---
    prev_count = _prev_forward_count()
    forward.to_csv(FWD_TRADES_CSV, index=False)
    days_live = (now_ms - freeze_ms) / 86_400_000
    fstats = _trade_stats(forward)
    runrow = {"run_utc": now.isoformat(), "days_live": round(days_live, 1),
              "latest_candle_utc": str(tdf["exit_dt"].max()), **fstats}
    pd.DataFrame([runrow]).to_csv(
        FWD_RUNLOG_CSV, mode="a", header=not os.path.exists(FWD_RUNLOG_CSV), index=False)

    # --- decision gate ---
    enough = fstats["trades"] >= DECISION_MIN_TRADES or days_live >= DECISION_MAX_DAYS
    pass_gate = (fstats["trades"] > 0 and fstats.get("pf", 0) >= DECISION_PF_BAR
                 and fstats.get("net_pnl_usd", 0) > 0)

    # --- Telegram (quiet: only on new forward trade or decision-ready) ---
    new_trades = prev_count is not None and fstats["trades"] > prev_count
    if enough:
        verdict = "✅ KEEP (edge holds)" if pass_gate else "❌ KILL (edge decayed)"
        _telegram(
            f"*Dual Thrust ETH — DECISION READY*\n"
            f"forward N={fstats['trades']}, PF {fstats.get('pf')} "
            f"(bar {DECISION_PF_BAR}), net ${fstats.get('net_pnl_usd')}\n{verdict}")
    elif new_trades:
        _telegram(
            f"*Dual Thrust ETH — forward update*\n"
            f"+{fstats['trades'] - prev_count} new (N={fstats['trades']}/"
            f"{DECISION_MIN_TRADES}, {days_live:.0f}d)\n"
            f"PF {fstats.get('pf')}, WR {fstats.get('win_rate')}, "
            f"net ${fstats.get('net_pnl_usd')}")

    print("=" * 70)
    print(f"DUAL THRUST ETH 6h — FORWARD PAPER RE-SIM   ({now.date()} UTC)")
    print("=" * 70)
    print(f"freeze={FREEZE_DATE}  days_live={days_live:.1f}  "
          f"latest_candle={tdf['exit_dt'].max()}")
    print(f"\nin-sample (pre-freeze, reference): {json.dumps(_trade_stats(insample))}")
    print(f"FORWARD  (post-freeze, OOS)      : {json.dumps(fstats)}")
    print(f"\nbacktest expectation: WR~40%, PF (in-sample) above, Sharpe 2.0")
    if not enough:
        need = DECISION_MIN_TRADES - fstats["trades"]
        print(f"\nSTATUS: ACCUMULATING — {fstats['trades']}/{DECISION_MIN_TRADES} "
              f"forward trades ({need} more) or {DECISION_MAX_DAYS}d. Keep running.")
    else:
        verdict = "KEEP (edge holds forward)" if pass_gate else "KILL (edge decayed)"
        print(f"\nDECISION READY: PF {fstats.get('pf')} vs {DECISION_PF_BAR} bar, "
              f"net ${fstats.get('net_pnl_usd')} -> {verdict}")
    print("=" * 70)
    return {"forward": fstats, "insample": _trade_stats(insample),
            "days_live": days_live, "enough": enough, "pass": pass_gate}


if __name__ == "__main__":
    run()
