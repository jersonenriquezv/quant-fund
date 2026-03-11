# Plan: Forward Test Rejected Setup Logging + Max Slippage Guard
> Status: PENDIENTE — implementar después de que el bot esté abriendo posiciones consistentemente
> Prerequisito: 1+ semana de operación live con trades reales

## Context
Claude AI filter is live but we can't measure if it adds alpha. Backtest showed AI v1 destroyed $5,454 of value (especially Setup B: 49%→21.4% WR). Prompt was recalibrated (v2 running). But backtests with incomplete historical data aren't the right way to evaluate Claude — **forward testing with real data is**. We need to: (1) log rejected setups with enough detail to simulate counterfactuals, and (2) fix a real execution gap (slippage guard).

---

## Feature 1: Forward Test Rejected Setup Logging (DESPUÉS de 1 semana live)

### What
Persist setup prices (entry, SL, TP1, TP2, confluences) alongside AI rejections in PostgreSQL, then build a script to simulate what would have happened if rejected trades had executed.

### Why
After 2-3 weeks of live data, answer: "Is Claude filtering more losers than winners?"

### Steps

1. Add columns to `ai_decisions` table → `data_service/data_store.py` `_create_tables()`
   - entry_price, sl_price, tp1_price, tp2_price (DOUBLE PRECISION), confluences (JSONB)

2. Update `insert_ai_decision()` → `data_service/data_store.py`
   - Accept and store the 5 new fields

3. Pass setup prices in `_persist_ai_decision()` → `main.py:231`

4. Pass setup prices in `_persist_ai_pre_filter()` → `main.py:346`

5. Create `scripts/evaluate_rejections.py` (new file)
   - Query rejected setups with prices from PostgreSQL
   - Simulate with candles after rejection: fill? SL or TP hit?
   - Report: wins, losses, filter accuracy %
   - CLI: `--days N`, `--verbose`

6. Update docs → `docs/context/03-ai-filter.md`, `docs/context/01-data-service.md`

---

## Feature 2: Max Slippage Guard (puede hacerse ya — 20 líneas)

### What
Close positions immediately if entry slippage exceeds `MAX_SLIPPAGE_PCT` (0.3%).

### Steps

1. Add `MAX_SLIPPAGE_PCT: float = 0.003` → `config/settings.py`

2. In `_on_entry_filled()` → `execution_service/monitor.py:335`
   - Insert BEFORE MIN_RISK_DISTANCE check
   - Calculate slippage, if > threshold: cancel SL/TP, market close, reason="excessive_slippage"
   - Skip in sandbox mode

3. Tests → `tests/test_execution.py::TestExcessiveSlippage`

4. Docs → `docs/context/05-execution.md`

---

## Risks
- Feature 1 needs enough rejections to be meaningful (2-3 weeks live)
- Slippage guard: 0.3% may need tuning after real fills observed
- Sandbox fills are synthetic — skip guard in sandbox

## Out of Scope
- Dashboard UI for rejection analysis
- Per-pair slippage thresholds
- Auto-schedule for evaluation script
