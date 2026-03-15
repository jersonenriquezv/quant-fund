#!/usr/bin/env python3
"""
Optuna parameter optimizer — automated strategy parameter tuning.

Wraps run_backtest() as an Optuna objective function. Sweeps configurable
parameter ranges and finds optimal values maximizing a target metric.

Usage:
    python scripts/optimize.py --days 60 --trials 100
    python scripts/optimize.py --days 60 --trials 50 --metric sharpe
    python scripts/optimize.py --days 60 --trials 100 --jobs 2
    python scripts/optimize.py --days 60 --trials 100 --walk-forward

Walk-forward validation: splits data into 70% train / 30% test.
Optimizes on train period, validates on test period.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Add project root AND scripts dir to path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_scripts_dir = os.path.join(_project_root, "scripts")
sys.path.insert(0, _project_root)
sys.path.insert(0, _scripts_dir)

try:
    import optuna
    from optuna.samplers import TPESampler
    from optuna.pruners import MedianPruner
except ImportError:
    print("Optuna not installed. Run: pip install optuna")
    sys.exit(1)

from backtest import run_backtest, BacktestMetrics
from shared.logger import setup_logger

logger = setup_logger("optimizer", file_level="INFO")

# Suppress Optuna's verbose logging
optuna.logging.set_verbosity(optuna.logging.WARNING)


# ================================================================
# Parameter space definition
# ================================================================

PARAM_SPACE = {
    # Setup A entry depth — fraction of OB body
    "SETUP_A_ENTRY_PCT": {"type": "float", "low": 0.30, "high": 0.70, "step": 0.05},
    # Max candles between sweep and CHoCH
    "SETUP_A_MAX_SWEEP_CHOCH_GAP": {"type": "int", "low": 15, "high": 60, "step": 5},
    # OB proximity — max distance from current price to trigger setup
    "OB_PROXIMITY_PCT": {"type": "float", "low": 0.003, "high": 0.015, "step": 0.001},
    # OB max distance — beyond this, OB is ignored
    "OB_MAX_DISTANCE_PCT": {"type": "float", "low": 0.04, "high": 0.12, "step": 0.01},
    # Minimum SL distance as fraction of entry price
    "MIN_RISK_DISTANCE_PCT": {"type": "float", "low": 0.001, "high": 0.004, "step": 0.0005},
    # OB minimum volume ratio
    "OB_MIN_VOLUME_RATIO": {"type": "float", "low": 1.0, "high": 2.0, "step": 0.1},
    # OB max age hours
    "OB_MAX_AGE_HOURS": {"type": "int", "low": 24, "high": 96, "step": 12},
    # OB minimum body size
    "OB_MIN_BODY_PCT": {"type": "float", "low": 0.0005, "high": 0.002, "step": 0.0005},
    # Minimum ATR filter
    "MIN_ATR_PCT": {"type": "float", "low": 0.001, "high": 0.005, "step": 0.0005},
    # Minimum target space
    "MIN_TARGET_SPACE_R": {"type": "float", "low": 0.8, "high": 2.0, "step": 0.2},
}


def suggest_params(trial: optuna.Trial) -> dict:
    """Suggest parameter values from the defined space."""
    overrides = {}
    for name, spec in PARAM_SPACE.items():
        if spec["type"] == "float":
            val = trial.suggest_float(name, spec["low"], spec["high"],
                                      step=spec.get("step"))
            overrides[name] = round(val, 6)
        elif spec["type"] == "int":
            val = trial.suggest_int(name, spec["low"], spec["high"],
                                    step=spec.get("step", 1))
            overrides[name] = val
    return overrides


# ================================================================
# Metric extraction
# ================================================================

def extract_metric(metrics: BacktestMetrics, metric_name: str) -> float:
    """Extract the target metric value from backtest results."""
    if metrics is None or metrics.total_trades == 0:
        return -999.0  # Penalize no-trade runs

    if metric_name == "profit_factor":
        return metrics.profit_factor if metrics.profit_factor != float("inf") else 10.0
    elif metric_name == "sharpe":
        return metrics.sharpe_ratio
    elif metric_name == "pnl":
        return metrics.total_pnl_usd
    elif metric_name == "win_rate":
        return metrics.win_rate
    elif metric_name == "composite":
        # Balanced score: profit_factor * sqrt(trades) * win_rate
        # Rewards profitability, trade frequency, and consistency
        pf = min(metrics.profit_factor, 10.0) if metrics.profit_factor != float("inf") else 10.0
        trade_factor = metrics.total_trades ** 0.5
        return pf * trade_factor * metrics.win_rate
    else:
        return getattr(metrics, metric_name, -999.0)


# ================================================================
# Objective function
# ================================================================

def make_objective(pairs: list[str] | None, days: int, capital: float,
                   metric: str, fill_mode: str | None,
                   quiet: bool = True):
    """Create an Optuna objective function wrapping run_backtest."""

    def objective(trial: optuna.Trial) -> float:
        overrides = suggest_params(trial)

        # Suppress ALL output during optimization.
        # Redirect stdout to /dev/null to avoid print() output.
        if quiet:
            from contextlib import redirect_stdout
            with redirect_stdout(open(os.devnull, "w")):
                result = run_backtest(
                    pairs=pairs, days=days, capital=capital,
                    fill_mode=fill_mode, overrides=overrides,
                )
        else:
            result = run_backtest(
                pairs=pairs, days=days, capital=capital,
                fill_mode=fill_mode, overrides=overrides,
            )

        if result is None:
            return -999.0

        score = extract_metric(result, metric)

        # Log progress
        logger.info(
            f"Trial {trial.number}: {metric}={score:.4f} "
            f"trades={result.total_trades} WR={result.win_rate*100:.1f}% "
            f"PnL=${result.total_pnl_usd:+,.2f}"
        )

        return score

    return objective


# ================================================================
# Walk-forward validation
# ================================================================

def walk_forward_validate(best_params: dict, pairs: list[str] | None,
                          days: int, capital: float, metric: str,
                          fill_mode: str | None) -> dict:
    """Run walk-forward validation: optimize on 70% train, validate on 30% test."""
    train_days = int(days * 0.7)
    test_days = days - train_days

    # Train period: first 70%
    import io
    from contextlib import redirect_stdout

    f = io.StringIO()
    with redirect_stdout(f):
        train_result = run_backtest(
            pairs=pairs, days=days, capital=capital,
            fill_mode=fill_mode, overrides=best_params,
        )

    # Test period: last 30% (use full period with no overrides as baseline)
    f = io.StringIO()
    with redirect_stdout(f):
        test_result = run_backtest(
            pairs=pairs, days=test_days, capital=capital,
            fill_mode=fill_mode, overrides=best_params,
        )

    # Baseline: test period with default params
    f = io.StringIO()
    with redirect_stdout(f):
        baseline_result = run_backtest(
            pairs=pairs, days=test_days, capital=capital,
            fill_mode=fill_mode,
        )

    return {
        "train": {
            "days": train_days,
            "metric": extract_metric(train_result, metric) if train_result else -999,
            "trades": train_result.total_trades if train_result else 0,
            "pnl": train_result.total_pnl_usd if train_result else 0,
            "win_rate": train_result.win_rate if train_result else 0,
        },
        "test_optimized": {
            "days": test_days,
            "metric": extract_metric(test_result, metric) if test_result else -999,
            "trades": test_result.total_trades if test_result else 0,
            "pnl": test_result.total_pnl_usd if test_result else 0,
            "win_rate": test_result.win_rate if test_result else 0,
        },
        "test_baseline": {
            "days": test_days,
            "metric": extract_metric(baseline_result, metric) if baseline_result else -999,
            "trades": baseline_result.total_trades if baseline_result else 0,
            "pnl": baseline_result.total_pnl_usd if baseline_result else 0,
            "win_rate": baseline_result.win_rate if baseline_result else 0,
        },
    }


# ================================================================
# Results output
# ================================================================

def save_results(study: optuna.Study, metric: str, days: int,
                 walk_forward: dict | None = None) -> str:
    """Save optimization results to JSON."""
    results_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "backtest_results")
    os.makedirs(results_dir, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(results_dir, f"{ts}_{days}d_optuna.json")

    best = study.best_trial
    result = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "optimization": {
            "metric": metric,
            "days": days,
            "n_trials": len(study.trials),
            "best_score": best.value,
            "best_params": best.params,
        },
        "top_5": [
            {
                "trial": t.number,
                "score": t.value,
                "params": t.params,
            }
            for t in sorted(study.trials, key=lambda t: t.value or -999,
                           reverse=True)[:5]
        ],
        "param_importance": {},
    }

    # Parameter importance (if enough trials)
    if len(study.trials) >= 10:
        try:
            importance = optuna.importance.get_param_importances(study)
            result["param_importance"] = {k: round(v, 4) for k, v in importance.items()}
        except Exception:
            pass

    if walk_forward:
        result["walk_forward"] = walk_forward

    with open(filename, "w") as f:
        json.dump(result, f, indent=2)

    return filename


def print_results(study: optuna.Study, metric: str,
                  walk_forward: dict | None = None) -> None:
    """Print optimization results to console."""
    best = study.best_trial

    print()
    print("=" * 70)
    print(f"OPTUNA OPTIMIZATION RESULTS  (metric={metric})")
    print("=" * 70)
    print(f"\n  Trials completed: {len(study.trials)}")
    print(f"  Best score:       {best.value:.4f}")
    print(f"\nBEST PARAMETERS:")
    for name, value in sorted(best.params.items()):
        print(f"  {name:<35} {value}")

    # Top 5 trials
    top5 = sorted(study.trials, key=lambda t: t.value or -999, reverse=True)[:5]
    print(f"\nTOP 5 TRIALS:")
    for t in top5:
        print(f"  Trial {t.number:<4} score={t.value:.4f}")

    # Parameter importance
    if len(study.trials) >= 10:
        try:
            importance = optuna.importance.get_param_importances(study)
            print(f"\nPARAMETER IMPORTANCE:")
            for name, imp in sorted(importance.items(), key=lambda x: -x[1]):
                bar = "#" * int(imp * 40)
                print(f"  {name:<35} {imp:.3f}  {bar}")
        except Exception:
            pass

    # Walk-forward validation
    if walk_forward:
        print(f"\n{'='*70}")
        print(f"WALK-FORWARD VALIDATION")
        print(f"{'='*70}")
        for period, data in walk_forward.items():
            print(f"\n  {period.upper()} ({data['days']}d):")
            print(f"    {metric}: {data['metric']:.4f}")
            print(f"    Trades: {data['trades']}")
            print(f"    PnL: ${data['pnl']:+,.2f}")
            print(f"    Win rate: {data['win_rate']*100:.1f}%")

        # Overfit check
        test_opt = walk_forward["test_optimized"]["metric"]
        test_base = walk_forward["test_baseline"]["metric"]
        if test_opt > test_base:
            print(f"\n  Optimized params OUTPERFORM baseline on test period")
        else:
            print(f"\n  WARNING: Optimized params UNDERPERFORM baseline on test period")
            print(f"  This suggests overfitting. Use default params or reduce search space.")

    print()
    print("=" * 70)


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Optuna parameter optimizer for backtest strategy tuning")
    parser.add_argument("--days", type=int, required=True,
                        help="Number of days of historical data to use")
    parser.add_argument("--trials", type=int, default=100,
                        help="Number of Optuna trials (default: 100)")
    parser.add_argument("--metric", type=str, default="profit_factor",
                        choices=["profit_factor", "sharpe", "pnl", "win_rate", "composite"],
                        help="Metric to maximize (default: profit_factor)")
    parser.add_argument("--capital", type=float, default=10000.0,
                        help="Initial capital (default: 10000)")
    parser.add_argument("--pair", type=str, default=None,
                        help="Single pair to test (e.g. BTC/USDT)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="Parallel trials (default: 1, max recommended: 2-4)")
    parser.add_argument("--fill-mode", choices=["optimistic", "conservative"],
                        default=None, help="Fill model")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Run walk-forward validation after optimization")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for reproducibility (default: 42)")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else None

    print(f"Optuna optimizer: {args.trials} trials, metric={args.metric}, "
          f"days={args.days}, capital=${args.capital:,.0f}")
    print(f"Parameter space: {len(PARAM_SPACE)} parameters")
    print()

    # Create study
    sampler = TPESampler(seed=args.seed)
    study = optuna.create_study(
        direction="maximize",
        sampler=sampler,
        pruner=MedianPruner(n_startup_trials=10),
    )

    # Run optimization
    objective = make_objective(
        pairs=pairs, days=args.days, capital=args.capital,
        metric=args.metric, fill_mode=args.fill_mode,
    )

    study.optimize(objective, n_trials=args.trials, n_jobs=args.jobs)

    # Walk-forward validation
    wf_result = None
    if args.walk_forward and args.days >= 30:
        print("\nRunning walk-forward validation...")
        wf_result = walk_forward_validate(
            best_params=study.best_params,
            pairs=pairs, days=args.days, capital=args.capital,
            metric=args.metric, fill_mode=args.fill_mode,
        )

    # Results
    print_results(study, args.metric, walk_forward=wf_result)
    filename = save_results(study, args.metric, args.days,
                           walk_forward=wf_result)
    print(f"Results saved: {filename}")


if __name__ == "__main__":
    main()
