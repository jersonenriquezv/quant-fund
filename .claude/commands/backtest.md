Run a backtest and organize results. Arguments are passed directly to the backtest script.

Default (no args): `--days 30 --detail --csv`

## Steps

1. `source venv/bin/activate && python scripts/backtest.py $ARGUMENTS 2>&1` — run the backtest (pass all user arguments through, use defaults if none provided)
2. Move any `backtest_results_*.csv` files from project root into `backtest_results/`
3. Report the summary table (trades, WR, PnL, PF, Sharpe, DD, per-setup breakdown)
4. Ask: "Add to TRACKER.md?" — if yes, append a row with date, filename, and key metrics

## Output format

Show the full summary table from the backtest output. Then:

```
Results: backtest_results/FILENAME.json
CSV: backtest_results/FILENAME.csv (moved from project root)
```

Do NOT modify strategy code. Do NOT read source files beyond the backtest output.
