# ML v0 — engine1_trend_pullback meta-label baseline

_Generated: 2026-06-15T19:02:13.846765Z. Issue: #25._

## Dataset

- **Setup type:** `engine1_trend_pullback`
- **Feature version filter:** `feature_version >= 4`
- **Outcome filter:** `shadow_tp` (label=1) or `shadow_sl` (label=0)
- **N total:** 297
- **Class balance:** 129 TP / 168 SL (43.4% positive)
- **Date range:** 2026-04-28 03:15:02.960949 → 2026-06-12 08:15:04.306946
- **Experiments included:** engine1_short_multipair_v1c_2026_05_07, engine1_short_quarantine_v1d_2026_05_22, redesign_pre_2026_04_27
- **Feature columns:** 99 (after dropping identity/outcome/timestamp)

## Split

- **Strategy:** time-sorted 80/20 holdout (no look-ahead)
- **Train N:** 238 (112 TP / 126 SL)
- **Test N:** 59 (17 TP / 42 SL)

## Metrics

- **AUC train:** 0.9909
- **AUC test:**  0.7815

## Verdict

**EDGE CLARO** — Continuar recolectando engine1, iterar modelo, no construir Engine 2 todavía.

_Confidence: Overfit gap (train=0.99, test=0.78) > 0.20. Model memorizes training set; test AUC at small N is unreliable._

| AUC test | Veredicto | Acción |
|---|---|---|
| > 0.60 | Edge claro | Continuar engine1, no construir Engine 2 |
| 0.55–0.60 | Señal débil | Recolectar más, re-train |
| 0.50–0.55 | Marginal | Construir Engine 2 |
| < 0.50 | Anti-edge | Audit |

## Top-15 Feature Importance (gain)

| Rank | Feature | Importance |
|---|---|---|
| 1 | `engine1_pullback_depth_pct` | 86.41 |
| 2 | `engine1_entry_atr_distance` | 69.77 |
| 3 | `engine1_impulse_atr_multiple` | 67.75 |
| 4 | `btc_volatility_ratio` | 41.62 |
| 5 | `wt_wt2` | 27.90 |
| 6 | `setup_age_minutes` | 25.10 |
| 7 | `fear_greed_score` | 23.89 |
| 8 | `btc_return_20` | 19.58 |
| 9 | `bb_percent_b` | 18.39 |
| 10 | `spread_bps` | 17.09 |
| 11 | `risk_distance_pct` | 16.94 |
| 12 | `cvd_1h` | 15.93 |
| 13 | `shadow_margin` | 14.72 |
| 14 | `stoch_rsi_k` | 14.48 |
| 15 | `whale_count` | 13.22 |

## Caveats

- N is small; AUC variance is wide. Re-train at N=200, N=300 to confirm trend.
- Multiple `experiment_id` regimes mixed — parameter shifts across rows may add noise.
- `shadow_breakeven` and `shadow_timeout` excluded. These contain useful info for
  multi-class classification later but are ambiguous for v0 binary.
- No hyperparameter tuning. Defaults chosen to be conservative against overfitting.
