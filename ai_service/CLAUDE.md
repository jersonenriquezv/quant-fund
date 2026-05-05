# AI Service — CLAUDE.md

Operational rules for Claude when modifying `ai_service/`. Currently **bypassed for all active setups** — code remains for future meta-labeling model.

## Purpose
LLM-based trade filter. Receives `TradeSetup` + `MarketSnapshot`, queries Claude (Sonnet) with scoring rubric, returns `AIDecision { confidence, approved, scores, factors, adjustments, warnings }`.

## Source of truth (read before editing)
- **Detailed behavior:** `docs/context/03-ai-filter.md` (Spanish, deep — prompt structure, scoring rubric v2, pre-filter, dedup, FAQ, ML roadmap)
- **Audit (2026-03-18):** `docs/audits/ai-service-audit-2026-03-18.md` — AFML roadmap (meta-labeling → bet sizing → sample weights)
- **Active config:** `docs/SYSTEM_BASELINE.md` — `AI_BYPASS_SETUP_TYPES`, `AI_MIN_CONFIDENCE`, model names
- **Models:** `shared/models.py` — `TradeSetup`, `MarketSnapshot`, `AIDecision`

## Status
**ZERO Claude API calls in live pipeline.** `ENABLED_SETUPS = []` — bot is shadow-only since 2026-04-15. All shadow setups (`setup_a`, `setup_b`, `setup_d_bos`, `setup_d_choch`, `setup_f`, `engine1_trend_pullback`, `bench_engine1_random_direction`, `bench_engine1_market_now`) skip AI by virtue of shadow routing — `main.py` short-circuits to `shadow_monitor.add_shadow(setup)` before AI/Risk/Execution. Engine 1 and its benchmarks bypass AI specifically because they are shadow-only; once promoted to live, AI gating must be reconsidered. `AI_BYPASS_SETUP_TYPES = ("setup_a", "setup_b", "setup_f")` and `QUICK_SETUP_TYPES` still synthesize `AIDecision(confidence=1.0, approved=True)` for any future live path. Pre-filter, prompt builder, and Claude client code stay intact for future re-enable.

Two ways the AI service IS used today:
- **`scripts/weekly_edge_audit.py`** — offline weekly audit over resolved `ml_setups` + `trades` + shadow outcomes. Uses `CLAUDE_MODEL_AUDIT` (env, default `claude-opus-4-7`). Runs Sunday 10:00 UTC via systemd timer. Persists to `ml_edge_audits` + `docs/audits/edge-audit-YYYY-WW.md`. **Does not touch live path.**
- **`scripts/pretrade_check.py`** + Telegram `/check` command — pre-trade Bybit checklist. Read-only, no order placement. Logs to `bybit_pretrade_checks`.

## Files
| File | Role |
|---|---|
| `service.py` | Facade `AIService`. `evaluate(setup, snapshot)` → `AIDecision`. Double-check approved AND confidence ≥ threshold. Fail-safe rejects on any error |
| `prompt_builder.py` | System + user prompts. Scoring rubric v2 (4 dimensions × 0-5). Neutral language (no Wyckoff/ICT narrative). Confluences tagged [SUPPORTING]/[CONTEXT] |
| `claude_client.py` | `AsyncAnthropic` wrapper. `temperature=0.3`, `max_tokens=500`, retries 2. Strips markdown code fences. Validates required JSON fields. Logs token usage every call |

## Rules — fail-safe is sacred
1. **Any failure → reject the trade.** API key missing, timeout, JSON invalid, missing fields, scores not dict, confidence outside [0,1] — all reject. **Never approve on uncertainty.**
2. **Double-check at facade.** `approved=True` AND `confidence >= AI_MIN_CONFIDENCE`. Claude's `approved=true` alone is not sufficient.
3. **Confidence is clamped to [0, 1]** in the facade, not in client.
4. **Bypass paths short-circuit BEFORE the service.** `main.py` decides bypass via `AI_BYPASS_SETUP_TYPES` / `QUICK_SETUP_TYPES`. Do not move bypass logic into `AIService.evaluate()`.

## Rules — modifying the prompt
1. **No interpretive narrative.** Funding = "directional crowding", not "liquidation fuel". Whales = "net exchange withdrawals reduce sell-side supply", not "bullish accumulation". Past bug: narrative framing let Claude rationalize bad approvals.
2. **Scoring rubric is mechanical.** 4 dimensions × 0-5. APPROVE: `setup_quality >= 3 AND contradiction <= 2 AND confidence >= threshold`. Do not soften. "Insufficient edge" is a valid reject.
3. **Absent data = neutral.** Do not penalize OR reward missing data. `data_sufficiency` score reflects how much was available.
4. **Threshold is dynamic.** System prompt is rebuilt per evaluation with current `AI_MIN_CONFIDENCE`. Do not hardcode.
5. **Confluences tagged [SUPPORTING] vs [CONTEXT].** Strategy emits both classes. Builder must tag correctly so Claude doesn't conflate.
6. **HTF position trade note** added when `setup.ob_timeframe in ("4h", "1h")`. Keep this — Claude's evaluation depends on knowing it's a swing not intraday.

## Rules — pre-filter (deterministic, before Claude)
1. **Three checks live in `main.py:_pre_filter_for_claude()`.** Funding extreme, F&G extreme, CVD divergence. Reject obvious losers without API tokens.
2. **Conservative on missing data.** If snapshot field is None → skip the check, don't reject.
3. **Pre-filter rejections persist** to `ai_decisions` with `approved=False, confidence=0.0, reasoning="Pre-filter: {reason}"`. Show in dashboard AILog.
4. **HTF bias conflict check was REMOVED.** HTF bias is now context for Claude, not a hard gate. Counter-trend setups with clear LTF structure are allowed.

## Rules — dedup cache
1. **Key:** `(pair, direction, setup_type)` (see `main.py:232`). Entry price is NOT part of the key — geometry shifts on the same setup do not produce a second signal within the TTL.
2. **TTL is dual:** live path uses `_SETUP_DEDUP_TTL_SECONDS = 3600` (1h, prevents re-sending while limit order is pending); shadow path uses `_SHADOW_DEDUP_TTL_SECONDS = 300` (5m, shadow is data collection so only same-candle repeats are suppressed). Selection is by `is_shadow` flag in `main.py`.
3. **Live cache updates after successful Claude / live-path processing. Shadow cache updates after `shadow_monitor.add_shadow()` attempt.** Pre-filter rejects do not pollute the live cache (they re-evaluate next candle).
4. **Cache covers ALL setup types**, including bypassed ones — prevents Telegram spam.

## Rules — adding new functionality
1. **Persist every decision** (approved or rejected) to `ai_decisions` via `main.py:_persist_ai_decision()`. Dashboard depends on it.
2. **Tokens are logged on every call:** `Claude tokens: input=X output=Y total=Z`. Do not remove — cost tracking depends on it.
3. **Adjustments (`AIDecision.adjustments`) carry scores AND optional SL/TP.** Risk and Execution do NOT read SL/TP adjustments yet. Document as v2 if you wire it.
4. **Use `CLAUDE_MODEL_AUDIT` for offline tools** (weekly edge audit, pre-trade check). Use `CLAUDE_MODEL` for the live filter. They are separately env-overridable for cost reasons.

## Never
- Re-enable AI in live path without recalibration. AI v2 had 89.6% approval rate = no value. Need new threshold + scoring before flipping `AI_BYPASS_SETUP_TYPES`.
- Add streaming. Response is 200-400 tokens — `await` is enough.
- Add fallback to another model. Different prompt + testing = complexity not worth it. Reject on Claude failure.
- Use AI live-path code from inside `weekly_edge_audit.py` — those run offline with different prompts.

## Verify after changes
```bash
python -m pytest tests/test_ai_service.py tests/test_prompt_builder.py tests/test_claude_client.py -v --tb=short
```

## Cost reference
~1,600 tokens input + ~200 tokens output ≈ $0.008/eval (Sonnet). Trivial at current volume but worth tracking.
