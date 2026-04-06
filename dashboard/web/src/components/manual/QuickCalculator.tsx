"use client";

import { useState, useEffect, useCallback } from "react";
import { postApi, fetchApi, putApi } from "@/lib/api";
import type { CalcResult, ManualBalance } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

const LINEAR_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT"];
const INVERSE_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "LINK/USD", "AVAX/USD"];

function coinFromPair(pair: string): string {
  return pair.split("/")[0];
}

interface SuggestedSL {
  suggested_sl: number | null;
  ob: { direction: string; high: number; low: number; body_high: number; body_low: number } | null;
}

export function QuickCalculator() {
  const [marginType, setMarginType] = useState<"linear" | "inverse">("linear");
  const [pair, setPair] = useState("BTC/USDT");
  const [direction, setDirection] = useState("long");
  const [entry, setEntry] = useState("");
  const [sl, setSl] = useState("");
  const [tp1, setTp1] = useState("");
  const [showTp2, setShowTp2] = useState(false);
  const [tp2, setTp2] = useState("");
  const [balance, setBalance] = useState("");
  const [balanceCurrency, setBalanceCurrency] = useState<"usd" | "coin">("usd");
  const [riskPct, setRiskPct] = useState("1");
  const [leverage, setLeverage] = useState("7");
  const [result, setResult] = useState<CalcResult | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [currentPrice, setCurrentPrice] = useState<number | null>(null);
  const [savedBalances, setSavedBalances] = useState<ManualBalance[]>([]);
  const [balanceSaved, setBalanceSaved] = useState(false);
  const [suggestedSL, setSuggestedSL] = useState<SuggestedSL | null>(null);
  const [loadingSL, setLoadingSL] = useState(false);

  const pairs = marginType === "linear" ? LINEAR_PAIRS : INVERSE_PAIRS;
  const coin = coinFromPair(pair);
  const isInverse = marginType === "inverse";

  // Suggested TPs (client-side, from entry + SL distance)
  const entryNum = parseFloat(entry) || 0;
  const slNum = parseFloat(sl) || 0;
  const slDist = entryNum && slNum ? Math.abs(entryNum - slNum) : 0;
  const sugTp1 = slDist > 0
    ? (direction === "long" ? entryNum + slDist : entryNum - slDist) : 0;
  const sugTp2 = slDist > 0
    ? (direction === "long" ? entryNum + 2 * slDist : entryNum - 2 * slDist) : 0;

  // Fetch real-time price
  useEffect(() => {
    let active = true;
    const fetchPrice = async () => {
      try {
        const res = await fetchApi<{ price: number }>(`/manual/price/${encodeURIComponent(pair)}`);
        if (active) setCurrentPrice(res.price);
      } catch {
        if (active) setCurrentPrice(null);
      }
    };
    fetchPrice();
    const interval = setInterval(fetchPrice, 10000);
    return () => { active = false; clearInterval(interval); };
  }, [pair]);

  // Load saved balances
  const loadBalances = useCallback(async () => {
    try {
      const res = await fetchApi<ManualBalance[]>("/manual/balances");
      setSavedBalances(res);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadBalances(); }, [loadBalances]);

  // Auto-populate balance when pair changes
  useEffect(() => {
    const saved = savedBalances.find((b) => b.pair === pair);
    if (saved) {
      setBalance(String(saved.balance));
      setBalanceCurrency(isInverse ? "coin" : "usd");
      setBalanceSaved(true);
    } else {
      setBalanceSaved(false);
    }
  }, [pair, savedBalances, isInverse]);

  const handleMarginTypeChange = (t: "linear" | "inverse") => {
    setMarginType(t);
    const newPair = t === "linear" ? LINEAR_PAIRS[0] : INVERSE_PAIRS[0];
    setPair(newPair);
    setBalanceCurrency(t === "inverse" ? "coin" : "usd");
    setResult(null);
    setSuggestedSL(null);
  };

  const fetchSuggestedSL = async () => {
    if (!entry) return;
    setLoadingSL(true);
    try {
      const res = await fetchApi<SuggestedSL>(
        `/manual/suggested-sl?pair=${encodeURIComponent(pair)}&direction=${direction}&entry=${parseFloat(entry)}`
      );
      setSuggestedSL(res);
      if (res.suggested_sl && !sl) {
        setSl(String(res.suggested_sl));
      }
    } catch {
      setSuggestedSL(null);
    }
    setLoadingSL(false);
  };

  const handleCalc = async () => {
    if (!entry || !sl || !balance) return;
    setLoading(true);
    setError("");
    try {
      const body: Record<string, unknown> = {
        pair,
        direction,
        entry: parseFloat(entry),
        stop_loss: parseFloat(sl),
        balance: parseFloat(balance),
        balance_currency: balanceCurrency,
        risk_percent: parseFloat(riskPct),
        leverage: parseInt(leverage),
        margin_type: marginType,
      };
      if (tp1) body.take_profit_1 = parseFloat(tp1);
      if (showTp2 && tp2) body.take_profit_2 = parseFloat(tp2);
      if (!showTp2) body.take_profit_2 = null;

      const res = await postApi<CalcResult>("/manual/calculate", body);
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
        entry: parseFloat(entry),
        stop_loss: parseFloat(sl),
        take_profit_1: result.take_profit_1,
        take_profit_2: result.take_profit_2,
        balance: parseFloat(balance),
        balance_currency: balanceCurrency,
        risk_percent: parseFloat(riskPct),
        leverage: parseInt(leverage),
        margin_type: marginType,
      });
      // Auto-save balance for this pair
      await saveBalance(true);
      setResult(null);
      setSuggestedSL(null);
      setEntry("");
      setSl("");
      setTp1("");
      setTp2("");
    } catch (e) {
      setError(String(e));
    }
    setCreating(false);
  };

  const saveBalance = async (silent = false) => {
    if (!balance) return;
    try {
      await putApi(`/manual/balances/${encodeURIComponent(pair)}`, {
        balance: parseFloat(balance),
      });
      setBalanceSaved(true);
      await loadBalances();
    } catch (e) {
      if (!silent) setError(String(e));
    }
  };

  const applySuggestedSL = () => {
    if (suggestedSL?.suggested_sl) {
      setSl(String(suggestedSL.suggested_sl));
      setResult(null);
    }
  };

  return (
    <div>
      <div className="card-title" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span>Calculator</span>
        {currentPrice && (
          <span className="manual-price-badge">
            {coin} ${fmt(currentPrice, currentPrice >= 100 ? 2 : currentPrice >= 1 ? 4 : 6)}
          </span>
        )}
      </div>
      <div className="manual-calc-form">
        {/* Margin type toggle */}
        <div className="manual-dir-toggle">
          <button
            className={`manual-dir-btn ${marginType === "linear" ? "manual-margin-active" : ""}`}
            onClick={() => handleMarginTypeChange("linear")}
          >Linear (USDT)</button>
          <button
            className={`manual-dir-btn ${marginType === "inverse" ? "manual-margin-active" : ""}`}
            onClick={() => handleMarginTypeChange("inverse")}
          >Inverse (USD)</button>
        </div>

        <div className="manual-calc-row">
          <select value={pair} onChange={(e) => { setPair(e.target.value); setResult(null); }} className="manual-input">
            {pairs.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <div className="manual-dir-toggle">
            <button
              className={`manual-dir-btn ${direction === "long" ? "manual-dir-active-long" : ""}`}
              onClick={() => { setDirection("long"); setResult(null); }}
            >Long</button>
            <button
              className={`manual-dir-btn ${direction === "short" ? "manual-dir-active-short" : ""}`}
              onClick={() => { setDirection("short"); setResult(null); }}
            >Short</button>
          </div>
        </div>

        {/* Entry + SL */}
        <div className="manual-calc-row">
          <input className="manual-input" type="number" placeholder="Entry price" value={entry}
            onChange={(e) => { setEntry(e.target.value); setResult(null); }} />
          <div style={{ display: "flex", flex: 1, gap: 4 }}>
            <input className="manual-input" type="number" placeholder="Stop loss" value={sl}
              onChange={(e) => { setSl(e.target.value); setResult(null); }} style={{ flex: 1 }} />
            <button
              className="manual-btn"
              onClick={fetchSuggestedSL}
              disabled={!entry || loadingSL}
              title="Suggest SL from nearest 4H order block"
              style={{ padding: "0 8px", fontSize: 11, whiteSpace: "nowrap" }}
            >
              {loadingSL ? "..." : "4H OB"}
            </button>
          </div>
        </div>

        {/* Suggested SL display */}
        {suggestedSL?.suggested_sl && (
          <div className="manual-suggested-sl" onClick={applySuggestedSL}>
            <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>
              4H OB SL: <strong style={{ color: "var(--short)" }}>${fmt(suggestedSL.suggested_sl)}</strong>
              {suggestedSL.ob && (
                <span style={{ marginLeft: 6, opacity: 0.7 }}>
                  ({suggestedSL.ob.direction} OB {fmt(suggestedSL.ob.low)}-{fmt(suggestedSL.ob.high)})
                </span>
              )}
              {sl !== String(suggestedSL.suggested_sl) && (
                <span style={{ marginLeft: 6, color: "var(--accent)", cursor: "pointer" }}>apply</span>
              )}
            </span>
          </div>
        )}
        {suggestedSL && !suggestedSL.suggested_sl && (
          <div style={{ fontSize: 11, color: "var(--warning)", marginTop: 4, padding: "4px 8px", background: "rgba(245,158,11,0.08)", borderRadius: 6 }}>
            No 4H {direction === "long" ? "bullish" : "bearish"} OB found for {pair}.
          </div>
        )}

        {/* TP1 + optional TP2 */}
        <div className="manual-calc-row">
          <input className="manual-input" type="number"
            placeholder={sugTp1 > 0 ? `TP1 (sug: ${fmt(sugTp1, 2)})` : "TP1 (optional)"}
            value={tp1}
            onChange={(e) => { setTp1(e.target.value); setResult(null); }}
          />
          {showTp2 ? (
            <>
              <input className="manual-input" type="number"
                placeholder={sugTp2 > 0 ? `TP2 (sug: ${fmt(sugTp2, 2)})` : "TP2"}
                value={tp2}
                onChange={(e) => { setTp2(e.target.value); setResult(null); }}
              />
              <button className="manual-tp2-toggle" onClick={() => { setShowTp2(false); setTp2(""); setResult(null); }}
                title="Remove TP2">
                &minus; TP2
              </button>
            </>
          ) : (
            <button className="manual-tp2-toggle" onClick={() => setShowTp2(true)}
              title="Add second take profit">
              + TP2
            </button>
          )}
        </div>

        {/* Balance + currency + save */}
        <div className="manual-calc-row">
          <div style={{ display: "flex", flex: 1, gap: 4, alignItems: "center" }}>
            <input className="manual-input" type="number"
              placeholder={`Balance (${isInverse && balanceCurrency === "coin" ? coin : "USDT"})`}
              value={balance}
              onChange={(e) => { setBalance(e.target.value); setBalanceSaved(false); setResult(null); }}
              style={{ flex: 1 }}
            />
            {isInverse && (
              <div className="manual-balance-currency">
                <button className={balanceCurrency === "coin" ? "active" : ""}
                  onClick={() => { setBalanceCurrency("coin"); setResult(null); }}>
                  {coin}
                </button>
                <button className={balanceCurrency === "usd" ? "active" : ""}
                  onClick={() => { setBalanceCurrency("usd"); setResult(null); }}>
                  USD
                </button>
              </div>
            )}
            <button
              className={`manual-save-btn ${balanceSaved ? "saved" : ""}`}
              onClick={() => saveBalance()}
              disabled={!balance}
              title="Save balance for this pair"
            >
              {balanceSaved ? "Saved" : "Save"}
            </button>
          </div>
        </div>

        {/* Balance conversion hint */}
        {isInverse && balanceCurrency === "coin" && currentPrice && balance && (
          <div style={{ fontSize: 10, color: "var(--text-muted)", padding: "0 4px" }}>
            {balance} {coin} &asymp; ${fmt(parseFloat(balance) * currentPrice)} USD at current price
          </div>
        )}

        <div className="manual-calc-row">
          <input className="manual-input" type="number" placeholder="Risk %" value={riskPct}
            onChange={(e) => { setRiskPct(e.target.value); setResult(null); }} />
          <input className="manual-input" type="number" placeholder="Leverage" value={leverage}
            onChange={(e) => { setLeverage(e.target.value); setResult(null); }} />
        </div>
        <button className="manual-btn manual-btn-calc" onClick={handleCalc} disabled={loading || !entry || !sl || !balance}>
          {loading ? "..." : "Calculate"}
        </button>
      </div>

      {error && <div style={{ color: "var(--short)", fontSize: 12, marginTop: 8 }}>{error}</div>}

      {result && (
        <div className="manual-calc-result">
          <div className="manual-calc-result-grid">
            <div>
              <span className="manual-stat-label">Size</span>
              <span>{isInverse ? fmt(result.position_size, 0) + " ct" : fmt(result.position_size, 4) + " " + coin}</span>
            </div>
            <div>
              <span className="manual-stat-label">Margin</span>
              <span>{isInverse ? fmt(result.margin_required, 6) + " " + coin : "$" + fmt(result.margin_required)}</span>
            </div>
            <div>
              <span className="manual-stat-label">Risk</span>
              <span style={{ color: "var(--short)" }}>${fmt(result.risk_usd)} ({fmt(result.risk_percent, 1)}%)</span>
            </div>
            <div>
              <span className="manual-stat-label">Leverage</span>
              <span>{result.leverage}x</span>
            </div>
            <div>
              <span className="manual-stat-label">SL dist</span>
              <span>{fmt(result.sl_distance_pct, 2)}%</span>
            </div>
            <div>
              <span className="manual-stat-label">Margin %</span>
              <span>{fmt(result.margin_pct_of_balance, 1)}%</span>
            </div>
          </div>

          {/* TP breakdown */}
          <div className="manual-tp-breakdown">
            {result.tp_plan[0] && (
              <div className="manual-tp-row">
                <span className="manual-tp-label">TP1</span>
                <span>${fmt(result.tp_plan[0].price)}</span>
                <span className="manual-tp-rr">{fmt(result.tp_plan[0].rr_ratio, 1)}R</span>
                <span style={{ color: "var(--long)" }}>+${fmt(result.tp_plan[0].potential_profit_usd)}</span>
              </div>
            )}
            {result.tp_plan[1] && (
              <div className="manual-tp-row">
                <span className="manual-tp-label">TP2</span>
                <span>${fmt(result.tp_plan[1].price)}</span>
                <span className="manual-tp-rr">{fmt(result.tp_plan[1].rr_ratio, 1)}R</span>
                <span style={{ color: "var(--long)" }}>+${fmt(result.tp_plan[1].potential_profit_usd)}</span>
              </div>
            )}
            <div className="manual-tp-total-row">
              <span>Potential profit</span>
              <span style={{ color: "var(--long)", fontWeight: 600 }}>+${fmt(result.total_potential_profit)}</span>
              <span style={{ color: "var(--short)" }}>Risk: -${fmt(result.total_potential_loss)}</span>
            </div>
          </div>

          {result.advice && result.advice.length > 0 && (
            <div className="manual-advice-list">
              {result.advice.map((a, i) => (
                <div key={i} className={`manual-advice manual-advice-${a.level}`}>
                  <div className="manual-advice-msg">{a.message}</div>
                  {a.action && <div className="manual-advice-action">{a.action}</div>}
                </div>
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
