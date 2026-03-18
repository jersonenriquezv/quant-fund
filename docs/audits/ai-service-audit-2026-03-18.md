# AI Service Audit — 2026-03-18

## Verdict: REDESIGN REQUIRED

The AI service is architecturally sound (fail-safe, structured I/O) but fundamentally misused:
an LLM filter pretending to be a quantitative model. It takes structured numeric features,
converts to prose, sends to Claude, asks for 0-5 scores, converts back to a number.
This is the opposite of what AFML prescribes.

## Context

- AI bypassed since 03-10 (AI v1 destroyed Setup B: 49% → 21.4% WR)
- AI v2: 89.6% approval rate = zero discrimination
- Data/strategy/risk services refined (audits 03-16 through 03-18)
- 40 ML features collected per setup, zero models trained
- Bet sizing config (Half-Kelly) added to settings.py but not wired

## Key Findings

### F1. LLM-as-filter is fundamentally wrong (AFML Ch. 1, 3)
- Pipeline: `float → string → LLM → int → bool` = information destruction
- Non-reproducible (temperature=0.3), unquantifiable error, no backtest path
- Same features could feed a trained classifier directly

### F2. Meta-labeling is the correct architecture (AFML Ch. 3)
- Primary model = strategy_service (detects setups, determines side)
- Secondary model should predict P(profit | setup features) → confidence
- Bet sizing = Kelly criterion on calibrated confidence
- Quote: "Meta-labeling allows us to build a secondary ML model that learns
  to determine the size of the bet, while the primary model determines the side."

### F3. 40 features collected, 0 models trained (AFML Ch. 8)
- Feature importance (MDI/MDA/SFI) never computed
- Which features predict outcomes is unknown
- Must segment by feature_version >= 4 (pre-v4 data corrupted by CVD bug, OI existence-only check, asymmetric funding)

### F4. Pre-filter belongs in strategy, not AI
- Deterministic gates (funding extreme, F&G, CVD) already partially duplicated in strategy after audit
- Meta-labeling model should receive ALL setups to learn full feature space

### F5. Prompt has structural bias
- Confluences pre-labeled [SUPPORTING]/[CONTEXT] = pre-judging evidence
- Factor Reading Guide tells Claude HOW to weight = deterministic rule engine with noise

### F6. Confidence uncalibrated (AFML Ch. 10)
- Claude's 0-1 score has unknown calibration
- Half-Kelly requires calibrated P(profit), not subjective score
- Must produce calibration curve before wiring bet sizing

### F7. Scoring rubric thresholds unvalidated (AFML Ch. 7)
- setup_quality >= 3 AND contradiction <= 2 chosen by intuition
- Should derive from purged k-fold CV on data

## Implementation Roadmap

### Phase 1: Feature Importance (AFML Ch. 8)
- Load ml_setups WHERE feature_version >= 4 AND outcome IS NOT NULL
- Label: 1 if filled_tp/filled_trailing, 0 if filled_sl/filled_timeout
- Train RandomForest, compute MDI + MDA + SFI
- Output: ranked feature list, identify top 10 and bottom 10
- Minimum data: 50+ labeled outcomes

### Phase 2: Meta-Labeling Model (AFML Ch. 3)
- Train classifier on top N features from Phase 1
- Validation: purged k-fold with embargo (AFML Ch. 7)
- Metric: log-loss (calibration), not just accuracy
- Deploy in ai_service/ replacing Claude call
- Target: AUC > 0.55 on out-of-sample

### Phase 3: Bet Sizing (AFML Ch. 10)
- Wire KELLY_FRACTION, BET_SIZE_MIN, BET_SIZE_MAX in risk_service
- margin = FIXED_TRADE_MARGIN × bet_size_factor(calibrated_confidence)
- Requires calibrated probability from Phase 2

### Phase 4: Sample Weights (AFML Ch. 4)
- Weight training samples by uniqueness (non-overlapping trades)
- Down-weight clustered setups

## What to Keep
- ClaudeClient (future qualitative analysis)
- PromptBuilder (Telegram trade summaries)
- Dedup cache
- ML feature extraction (core of new pipeline)
- Bet sizing config
- Fail-safe pattern (model fail = reject)

## Data Segmentation

| Version | Dates | Status | Reason |
|---------|-------|--------|--------|
| v1 | pre 03-17 | DO NOT USE | CVD in contracts (not base), OI existence-only, asymmetric funding |
| v2 | 03-17 | DO NOT USE | Progressive trailing ON, but CVD still wrong |
| v3 | 03-17 to 03-18 | DO NOT USE | CVD fixed but OB vol=1.0 (disabled), ATR=0.20% (too low) |
| v4 | 03-18+ | TRAINING READY | All audit fixes applied: CVD divergence, OI delta, symmetric funding, OB vol 1.3, ATR 0.35% |
