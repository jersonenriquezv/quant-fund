"""Engine 2 Phase 1 — Dual Thrust fixed-param revalidation on OKX.

Tracer bullet: the Sharpe-1.72 Dual Thrust edge was fit on Binance candles.
This re-runs the SAME rule with FIXED winner params on OKX ETH-USDT-SWAP candles.

Two legs:
  1. FIDELITY  — replay on Binance 6h (aggregated from jesse_db 1m) in this pandas
                 harness; must reproduce Jesse's ~1.72 Sharpe within +-0.2.
  2. OKX GATE  — same harness on OKX ETH-USDT-SWAP 6h; gate = Sharpe>=1.2 AND
                 net%>0 AND trades>=80 over 2024-06-12 -> 2026-06-11.

Rule faithfully mirrors strategies/DUAL_THRUST/__init__.py, including its column
quirk (down_max_high reads the LOW column). Jesse candle cols = [ts,open,close,
high,low,vol]. Execution mirrors the Jesse lifecycle: signals at bar close,
market entries/flips fill at the NEXT bar open, SL fills intrabar, and a flip
re-enters one bar after the liquidation (go-flat then re-evaluate). OKX 6h bars
are fetched UTC-aligned ('6Hutc'); the Hong-Kong '6H' variant misaligns the 1D
anchor and collapses the result (Sharpe 0.21 vs 2.0). No funding (Phase 2).
Fee 0.05% per side.
"""
from __future__ import annotations

import os
import sys
import math
import json

import numpy as np
import pandas as pd
import psycopg2

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
HP = {
    "stop_loss_atr_rate": 1.6452234302490119,
    "down_length": 10,
    "up_length": 3,
    "down_coeff": 0.30106933690452525,
    "up_coeff": 0.8910825165430803,
}
RISK_PCT = 2.0            # Jesse utils.risk_to_qty(balance, 2, ...)
START_BALANCE = 10_000.0
FEE_RATE = 0.0005         # 0.05% per side
ATR_PERIOD = 14
WINDOW_START = "2024-06-12"
WINDOW_END = "2026-06-11"
ANCHOR_TF = "1D"
TRADE_TF = "6h"

CACHE_DIR = os.path.join(os.path.dirname(__file__), "storage", "okx")
JESSE_TARGET_SHARPE = 1.7232652969366977

# ----------------------------------------------------------------------------
# Data loaders
# ----------------------------------------------------------------------------
def _resample(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate 1m OHLCV -> rule (e.g. '6h','1D'), UTC-aligned, closed/label left."""
    idx = pd.to_datetime(df1m["timestamp"], unit="ms", utc=True)
    s = df1m.set_index(idx)
    agg = s.resample(rule, label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna()
    agg["timestamp"] = (agg.index.values.astype("datetime64[ns]").astype("int64") // 1_000_000)
    return agg.reset_index(drop=True)


def _jesse_pg_env() -> dict:
    """POSTGRES_* from the jesse-research .env (outside this repo); real env wins.
    No secret is hardcoded here — the password must come from .env or the
    environment, else we fail loudly."""
    path = os.path.expanduser("~/jesse-research/project/.env")
    vals = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    vals.update({k: v for k, v in os.environ.items() if k.startswith("POSTGRES_")})
    return vals


def load_binance_1m(symbol: str = "ETH-USDT") -> pd.DataFrame:
    env = _jesse_pg_env()
    password = env.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD not set — export it or add it to "
            "~/jesse-research/project/.env")
    conn = psycopg2.connect(
        host=env.get("POSTGRES_HOST", "localhost"),
        dbname=env.get("POSTGRES_NAME", "jesse_db"),
        user=env.get("POSTGRES_USERNAME", "jer"),
        password=password,
        port=int(env.get("POSTGRES_PORT", "5432")),
    )
    q = (
        "SELECT timestamp, open, high, low, close, volume FROM candle "
        "WHERE exchange='Binance Perpetual Futures' AND symbol=%s AND timeframe='1m' "
        "ORDER BY timestamp"
    )
    df = pd.read_sql(q, conn, params=(symbol,))
    conn.close()
    return df


def load_okx_6h(inst_id: str = "ETH-USDT-SWAP", count: int = 3100) -> pd.DataFrame:
    """Fetch OKX 6h directly from the v5 public REST (bypasses ccxt market-load
    quirk: ccxt.okx.load_markets() keysort crashes on None inst ids). Cache CSV."""
    import time
    import requests
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{inst_id}_6h.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache)

    url = "https://www.okx.com/api/v5/market/history-candles"
    rows, after = [], None
    while len(rows) < count:
        params = {"instId": inst_id, "bar": "6Hutc", "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX error: {data}")
        batch = data["data"]  # [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm], desc
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1][0]  # oldest ts -> fetch older next
        time.sleep(0.15)
    # Top up with the recent endpoint — history-candles lags ~1-2 days, and
    # forward validation needs the freshest closed bars.
    try:
        rec = requests.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": inst_id, "bar": "6Hutc", "limit": "300"}, timeout=15,
        ).json()
        if rec.get("code") == "0":
            rows.extend(rec["data"])
    except Exception:
        pass
    df = pd.DataFrame(
        [{"timestamp": int(x[0]), "open": float(x[1]), "high": float(x[2]),
          "low": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
         for x in rows if x[8] == "1"]  # confirmed only
    ).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df

# ----------------------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------------------
def wilder_atr(high, low, close, period=ATR_PERIOD):
    high, low, close = map(np.asarray, (high, low, close))
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    atr = np.full_like(tr, np.nan, dtype=float)
    if len(tr) < period:
        return atr
    atr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr

# ----------------------------------------------------------------------------
# Backtest
# ----------------------------------------------------------------------------
def backtest(df6: pd.DataFrame, df1d: pd.DataFrame, hp=HP, funding=None):
    """Faithful pandas Dual Thrust. Returns (metrics, trades, equity_df).

    Execution model mirrors Jesse: signals computed at bar close, market
    entries/flips fill at the NEXT bar open; SL fills intrabar (stop touch);
    ATR(stop) sizing risks RISK_PCT of balance.

    funding: optional (ts_ms ndarray, rate ndarray) of OKX 8h funding events.
    When given, each trade is charged funding over every event in its hold:
    cost = side * rate * notional (long pays when rate>0). notional ~ qty*entry.
    """
    f_ts = f_rate = None
    if funding is not None:
        f_ts, f_rate = funding
    o = df6["open"].to_numpy(float)
    h = df6["high"].to_numpy(float)
    low = df6["low"].to_numpy(float)
    c = df6["close"].to_numpy(float)
    ts = df6["timestamp"].to_numpy()
    atr = wilder_atr(h, low, c)

    # Map each 6h bar -> the open of its UTC day (anchor candle open, no lookahead).
    day_open = {int(r.timestamp): float(r.open) for r in df1d.itertuples()}
    bar_day = (pd.to_datetime(ts, unit="ms", utc=True).normalize().values.astype("datetime64[ns]").astype("int64") // 1_000_000)

    upL, dnL = hp["up_length"], hp["down_length"]
    upC, dnC = hp["up_coeff"], hp["down_coeff"]
    warm = max(upL, dnL, ATR_PERIOD)

    balance = START_BALANCE
    pos = None  # dict: side(+1/-1), entry, stop, qty, entry_ts, atr_at_entry
    trades = []
    eq_ts, eq_val = [], []
    # pending action computed at bar i-1 close, executed at bar i open.
    # Mirrors Jesse lifecycle: in-position opposite signal -> liquidate (go flat),
    # then re-evaluate FLAT a bar later before re-entering (1-bar gap on flips).
    #   pend = None | ("close",) | ("enter", side, atr)
    pend = None

    def close_pos(exit_price, reason, bar_ts):
        nonlocal balance, pos
        bal_before = balance
        pnl = pos["qty"] * (exit_price - pos["entry"]) * pos["side"]
        fees = (pos["entry"] + exit_price) * pos["qty"] * FEE_RATE
        funding_cost = 0.0
        if f_ts is not None:
            mask = (f_ts > pos["entry_ts"]) & (f_ts <= bar_ts)
            if mask.any():
                notional = pos["qty"] * pos["entry"]
                funding_cost = float(pos["side"] * f_rate[mask].sum() * notional)
        net = pnl - fees - funding_cost
        balance += net
        trades.append({
            "entry_ts": pos["entry_ts"], "exit_ts": bar_ts, "side": pos["side"],
            "entry": pos["entry"], "exit": exit_price, "qty": pos["qty"],
            "pnl_net": net, "funding_cost": funding_cost, "reason": reason,
            "ret": net / bal_before if bal_before else 0.0,
        })
        pos = None

    def open_pos(side, price, a, bar_ts):
        nonlocal pos
        stop = price - side * a * hp["stop_loss_atr_rate"]
        qty = (balance * RISK_PCT / 100.0) / abs(price - stop)
        pos = {"side": side, "entry": price, "stop": stop, "qty": qty,
               "entry_ts": bar_ts}

    for i in range(warm, len(df6)):
        # --- (a) execute pending action at THIS bar's open ---
        if pend is not None:
            if pend[0] == "close" and pos is not None:
                close_pos(o[i], "flip", ts[i])             # liquidate at open
            elif pend[0] == "enter" and pos is None:
                open_pos(pend[1], o[i], pend[2], ts[i])     # enter at open
            pend = None

        # --- (b) intrabar SL on the open position (incl. just-entered) ---
        if pos is not None:
            if pos["side"] == 1 and low[i] <= pos["stop"]:
                close_pos(pos["stop"], "sl", ts[i])
            elif pos["side"] == -1 and h[i] >= pos["stop"]:
                close_pos(pos["stop"], "sl", ts[i])

        # --- (c) compute signal at THIS bar's close for next-bar execution ---
        price = c[i]
        a = atr[i]
        anchor_open = day_open.get(int(bar_day[i]))
        if anchor_open is None or math.isnan(a):
            pend = None
            eq_ts.append(ts[i]); eq_val.append(balance + _unreal(pos, price))
            continue

        # thresholds (mirror source incl. down_max_high = max(low) quirk)
        up_close = c[i - upL + 1:i + 1]
        up_high = h[i - upL + 1:i + 1]
        up_low = low[i - upL + 1:i + 1]
        dn_close = c[i - dnL + 1:i + 1]
        dn_low = low[i - dnL + 1:i + 1]

        up_thrust = anchor_open + upC * max(
            up_close.max() - up_low.min(), up_high.max() - up_close.min())
        down_thrust = anchor_open - dnC * max(
            dn_close.max() - dn_low.min(), dn_low.max() - dn_close.min())  # quirk: low col

        long_cond = price > up_thrust
        short_cond = price < down_thrust
        sig = 1 if long_cond else (-1 if short_cond else 0)
        if pos is not None:
            # in position: only schedule a liquidation on an opposite signal.
            pend = ("close",) if (sig != 0 and sig != pos["side"]) else None
        else:
            # flat: schedule an entry on any signal (re-entry after a flip
            # therefore lands one bar later than the liquidation).
            pend = ("enter", sig, a) if sig != 0 else None

        eq_ts.append(ts[i]); eq_val.append(balance + _unreal(pos, price))

    equity = pd.DataFrame({"timestamp": eq_ts, "equity": eq_val})
    metrics = _metrics(equity, trades)
    return metrics, trades, equity


def _unreal(pos, price):
    if pos is None:
        return 0.0
    return pos["qty"] * (price - pos["entry"]) * pos["side"]


def _metrics(equity: pd.DataFrame, trades: list) -> dict:
    eq = equity.copy()
    eq.index = pd.to_datetime(eq["timestamp"], unit="ms", utc=True)
    daily = eq["equity"].resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * math.sqrt(365)) if rets.std() > 0 else 0.0
    final = eq["equity"].iloc[-1] if len(eq) else START_BALANCE
    net_pct = (final / START_BALANCE - 1.0) * 100.0
    roll_max = eq["equity"].cummax()
    max_dd = ((eq["equity"] - roll_max) / roll_max).min() * 100.0
    wins = sum(1 for t in trades if t["pnl_net"] > 0)
    n = len(trades)
    return {
        "sharpe": round(sharpe, 4),
        "net_pct": round(net_pct, 2),
        "max_dd_pct": round(float(max_dd), 2),
        "trades": n,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "final_balance": round(final, 2),
    }

# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------
def _slice_window(df):
    lo = pd.Timestamp(WINDOW_START, tz="UTC").value // 1_000_000
    hi = pd.Timestamp(WINDOW_END, tz="UTC").value // 1_000_000 + 86_400_000
    return df[(df["timestamp"] >= lo) & (df["timestamp"] < hi)].reset_index(drop=True)


def run():
    print("=" * 70)
    print("LEG 1 — FIDELITY (Binance 6h from jesse_db 1m)")
    print("=" * 70)
    b1m = load_binance_1m("ETH-USDT")
    b6 = _resample(b1m, "6h")
    b1d = _resample(b1m, "1D")
    # window: keep warmup before, slice trades window for reporting parity
    bw6 = b6[b6["timestamp"] < (pd.Timestamp(WINDOW_END, tz="UTC").value // 1_000_000 + 86_400_000)]
    bw6 = bw6[bw6["timestamp"] >= (pd.Timestamp(WINDOW_START, tz="UTC").value // 1_000_000
                                   - 60 * 86_400_000)].reset_index(drop=True)
    bm, _, _ = backtest(bw6, b1d)
    print(json.dumps(bm, indent=2))
    diff = abs(bm["sharpe"] - JESSE_TARGET_SHARPE)
    fidelity_ok = diff <= 0.2
    print(f"\nJesse target Sharpe={JESSE_TARGET_SHARPE:.3f}  harness={bm['sharpe']:.3f}  "
          f"|diff|={diff:.3f}  -> fidelity {'PASS' if fidelity_ok else 'FAIL'}")

    print("\n" + "=" * 70)
    print("LEG 2 — OKX GATE (ETH/USDT:USDT 6h)")
    print("=" * 70)
    okx6_full = load_okx_6h("ETH-USDT-SWAP", 3100)
    okx1d = _resample_ohlc(okx6_full, "1D")
    okx6 = okx6_full[okx6_full["timestamp"] >=
                     (pd.Timestamp(WINDOW_START, tz="UTC").value // 1_000_000
                      - 60 * 86_400_000)]
    okx6 = okx6[okx6["timestamp"] < (pd.Timestamp(WINDOW_END, tz="UTC").value // 1_000_000
                                     + 86_400_000)].reset_index(drop=True)
    om, otrades, _ = backtest(okx6, okx1d)
    print(json.dumps(om, indent=2))
    gate_ok = om["sharpe"] >= 1.2 and om["net_pct"] > 0 and om["trades"] >= 80
    print(f"\nGATE: Sharpe>=1.2 AND net%>0 AND trades>=80 -> "
          f"{'PASS' if gate_ok else 'FAIL (KILL)'}")

    print("\n" + "=" * 70)
    print(f"VERDICT: fidelity={'PASS' if fidelity_ok else 'FAIL'}  "
          f"okx_gate={'PASS' if gate_ok else 'KILL'}")
    print("=" * 70)
    return {"binance": bm, "okx": om, "fidelity_ok": fidelity_ok, "gate_ok": gate_ok}


def _resample_ohlc(df_bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate already-OHLC bars (e.g. 6h -> 1D)."""
    idx = pd.to_datetime(df_bars["timestamp"], unit="ms", utc=True)
    s = df_bars.set_index(idx)
    agg = s.resample(rule, label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    ).dropna()
    agg["timestamp"] = (agg.index.values.astype("datetime64[ns]").astype("int64") // 1_000_000)
    return agg.reset_index(drop=True)


# ----------------------------------------------------------------------------
# Phase 2 — funding + Monte Carlo
# ----------------------------------------------------------------------------
def load_okx_funding(inst_id: str = "ETH-USDT-SWAP", count: int = 2400):
    """OKX v5 funding-rate-history (8h cadence). Cache CSV. Returns DataFrame
    [timestamp(ms), rate]."""
    import time
    import requests
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{inst_id}_funding.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache)
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    rows, after = [], None
    while len(rows) < count:
        params = {"instId": inst_id, "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        data = requests.get(url, params=params, timeout=15).json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX funding error: {data}")
        batch = data["data"]
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1]["fundingTime"]
        time.sleep(0.15)
    df = pd.DataFrame(
        [{"timestamp": int(x["fundingTime"]), "rate": float(x["realizedRate"] or x["fundingRate"])}
         for x in rows]
    ).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    df.to_csv(cache, index=False)
    return df


def mc_trade_shuffle(rets, n=1000, seed=42):
    """Bootstrap trade-order shuffle. Compounds per-trade returns in random
    order; reports P(loss) and p95 (worst-5%) drawdown."""
    rng = np.random.default_rng(seed)
    rets = np.asarray(rets, float)
    finals, dds = [], []
    for _ in range(n):
        r = rng.permutation(rets)
        eq = START_BALANCE * np.cumprod(1.0 + r)
        finals.append(eq[-1])
        peak = np.maximum.accumulate(np.concatenate([[START_BALANCE], eq]))
        dd = ((np.concatenate([[START_BALANCE], eq]) - peak) / peak).min()
        dds.append(dd)
    finals = np.array(finals); dds = np.array(dds)
    return {
        "n": n,
        "prob_loss": round(float((finals < START_BALANCE).mean()), 4),
        "final_p5": round(float(np.percentile(finals, 5)), 2),
        "final_p50": round(float(np.percentile(finals, 50)), 2),
        "max_dd_p95_pct": round(float(np.percentile(dds, 5) * 100), 2),  # worst 5%
    }


def _okx_window(okx6_full):
    lo = pd.Timestamp(WINDOW_START, tz="UTC").value // 1_000_000 - 60 * 86_400_000
    hi = pd.Timestamp(WINDOW_END, tz="UTC").value // 1_000_000 + 86_400_000
    w = okx6_full[(okx6_full["timestamp"] >= lo) & (okx6_full["timestamp"] < hi)]
    return w.reset_index(drop=True)


def run_phase2():
    print("=" * 70)
    print("PHASE 2 — funding + Monte Carlo (OKX ETH-USDT-SWAP 6h)")
    print("=" * 70)
    okx6_full = load_okx_6h("ETH-USDT-SWAP", 3100)
    okx1d = _resample_ohlc(okx6_full, "1D")
    okx6 = _okx_window(okx6_full)
    fdf = load_okx_funding("ETH-USDT-SWAP", 2400)
    funding = (fdf["timestamp"].to_numpy(), fdf["rate"].to_numpy())

    # --- no-funding baseline (Phase 1 result, for reference) ---
    base_m, base_tr, _ = backtest(okx6, okx1d)
    # --- funding-adjusted ---
    m, tr, _ = backtest(okx6, okx1d, funding=funding)
    print(f"\nfunding events in window: {len(fdf)}  "
          f"rate mean={fdf['rate'].mean():.6f}  median={fdf['rate'].median():.6f}")
    print("\nNO funding :", json.dumps(base_m))
    print("WITH funding:", json.dumps(m))

    tdf = pd.DataFrame(tr)
    tot_funding = tdf["funding_cost"].sum()
    print(f"\ntotal funding paid $ {tot_funding:.1f}  "
          f"(net no-fund $ {base_m['final_balance']-START_BALANCE:.0f} -> "
          f"with-fund $ {m['final_balance']-START_BALANCE:.0f})")

    # held-time distribution (funding drag concentration check)
    hold_h = (tdf["exit_ts"] - tdf["entry_ts"]) / 3_600_000
    print(f"hold hours: median {hold_h.median():.1f}  p90 {hold_h.quantile(0.9):.1f}  "
          f"max {hold_h.max():.1f}")

    # --- MC trade-shuffle on funding-adjusted per-trade returns ---
    mc = mc_trade_shuffle(tdf["ret"].to_numpy(), n=1000)
    print("\nMC trade-shuffle (funding-adj):", json.dumps(mc))

    g_perf = m["sharpe"] >= 1.0 and m["net_pct"] > 0
    g_mc = mc["prob_loss"] <= 0.10
    print("\n" + "=" * 70)
    print(f"GATE perf: funding-adj Sharpe>=1.0 AND net%>0 -> "
          f"{'PASS' if g_perf else 'FAIL'}  (Sharpe {m['sharpe']}, net {m['net_pct']}%)")
    print(f"GATE MC  : P(loss)<=0.10 -> {'PASS' if g_mc else 'FAIL'}  "
          f"(P_loss {mc['prob_loss']})")
    print(f"VERDICT  : {'PROCEED-TO-PORT' if (g_perf and g_mc) else 'KILL'}")
    print("=" * 70)
    return {"base": base_m, "funding": m, "mc": mc,
            "proceed": bool(g_perf and g_mc)}


if __name__ == "__main__":
    import sys as _sys
    if "--phase2" in _sys.argv:
        run_phase2()
    else:
        run()
