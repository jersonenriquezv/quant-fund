"use client";

import { useState } from "react";
import { postApi } from "@/lib/api";
import type { CalcResult } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

const PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT"];

export function QuickCalculator() {
  const [pair, setPair] = useState("BTC/USDT");
  const [direction, setDirection] = useState("long");
  const [entry, setEntry] = useState("");
  const [sl, setSl] = useState("");
  const [balance, setBalance] = useState("500");
  const [riskPct, setRiskPct] = useState("1");
  const [leverage, setLeverage] = useState("7");
  const [result, setResult] = useState<CalcResult | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);

  const handleCalc = async () => {
    if (!entry || !sl) return;
    setLoading(true);
    setError("");
    try {
      const res = await postApi<CalcResult>("/manual/calculate", {
        pair,
        direction,
        entry_price: parseFloat(entry),
        sl_price: parseFloat(sl),
        balance: parseFloat(balance),
        risk_percent: parseFloat(riskPct),
        leverage: parseInt(leverage),
        margin_type: "linear",
      });
      setResult(res);
    } catch (e) {
      setError(String(e));
      setResult(null);
    }
    setLoading(false);
  };

  const handleCreate = async () => {
    if (!result) return;
    setCreating(true);
    try {
      await postApi("/manual/trades", {
        pair,
        direction,
        entry_price: parseFloat(entry),
        sl_price: parseFloat(sl),
        tp1_price: result.tp_plan.tp1_price,
        tp2_price: result.tp_plan.tp2_price,
        balance: parseFloat(balance),
        risk_percent: parseFloat(riskPct),
        leverage: parseInt(leverage),
        margin_type: "linear",
      });
      setResult(null);
      setEntry("");
      setSl("");
    } catch (e) {
      setError(String(e));
    }
    setCreating(false);
  };

  return (
    <div>
      <div className="card-title">Calculator</div>
      <div className="manual-calc-form">
        <div className="manual-calc-row">
          <select value={pair} onChange={(e) => setPair(e.target.value)} className="manual-input">
            {PAIRS.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <div className="manual-dir-toggle">
            <button
              className={`manual-dir-btn ${direction === "long" ? "manual-dir-active-long" : ""}`}
              onClick={() => setDirection("long")}
            >Long</button>
            <button
              className={`manual-dir-btn ${direction === "short" ? "manual-dir-active-short" : ""}`}
              onClick={() => setDirection("short")}
            >Short</button>
          </div>
        </div>
        <div className="manual-calc-row">
          <input
            className="manual-input"
            type="number"
            placeholder="Entry price"
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
          />
          <input
            className="manual-input"
            type="number"
            placeholder="Stop loss"
            value={sl}
            onChange={(e) => setSl(e.target.value)}
          />
        </div>
        <div className="manual-calc-row">
          <input
            className="manual-input"
            type="number"
            placeholder="Balance"
            value={balance}
            onChange={(e) => setBalance(e.target.value)}
          />
          <input
            className="manual-input"
            type="number"
            placeholder="Risk %"
            value={riskPct}
            onChange={(e) => setRiskPct(e.target.value)}
          />
          <input
            className="manual-input"
            type="number"
            placeholder="Leverage"
            value={leverage}
            onChange={(e) => setLeverage(e.target.value)}
          />
        </div>
        <button className="manual-btn manual-btn-calc" onClick={handleCalc} disabled={loading}>
          {loading ? "..." : "Calculate"}
        </button>
      </div>

      {error && <div style={{ color: "var(--short)", fontSize: 12, marginTop: 8 }}>{error}</div>}

      {result && (
        <div className="manual-calc-result">
          <div className="manual-calc-result-grid">
            <div>
              <span className="manual-stat-label">Size</span>
              <span>{fmt(result.position_size, 4)}</span>
            </div>
            <div>
              <span className="manual-stat-label">Margin</span>
              <span>${fmt(result.margin_required)}</span>
            </div>
            <div>
              <span className="manual-stat-label">Risk</span>
              <span style={{ color: "var(--short)" }}>${fmt(result.risk_usd)}</span>
            </div>
            <div>
              <span className="manual-stat-label">R:R</span>
              <span>{fmt(result.rr_ratio, 1)}</span>
            </div>
            <div>
              <span className="manual-stat-label">TP1</span>
              <span style={{ color: "var(--long)" }}>{fmt(result.tp_plan.tp1_price)}</span>
            </div>
            <div>
              <span className="manual-stat-label">TP2</span>
              <span style={{ color: "var(--long)" }}>{fmt(result.tp_plan.tp2_price)}</span>
            </div>
          </div>
          {result.warnings.length > 0 && (
            <div style={{ marginTop: 8 }}>
              {result.warnings.map((w, i) => (
                <div key={i} style={{ color: "var(--warning)", fontSize: 11 }}>{w}</div>
              ))}
            </div>
          )}
          <button className="manual-btn manual-btn-create" onClick={handleCreate} disabled={creating}>
            {creating ? "..." : "Create Trade"}
          </button>
        </div>
      )}
    </div>
  );
}
