"""Regression tests for restart-safety SQL filters in data_store.

Audit fase 0 #4: synthetic 'orphaned_restart' closes must NOT contaminate
DD reconcile, dashboard stats, or edge-audit. Every query that aggregates
pnl/stats from the trades table is required to exclude them.

These tests inspect the SQL emitted by each fetch method to guarantee the
filter is present. If a future refactor drops the filter from any query,
the regression fails loudly instead of silently polluting capital metrics.
"""

from unittest.mock import MagicMock

import pytest

from data_service.data_store import (
    NON_MARKET_OUTCOMES,
    PostgresStore,
    VALID_OUTCOMES,
    ml_market_outcome_filter_sql,
)


def _build_store(fetchone=(0.0, 0), fetchall=()):
    """PostgresStore with its cursor mocked. Bypass __init__ to avoid
    touching real connection state."""
    store = PostgresStore.__new__(PostgresStore)
    store._conn = MagicMock()
    store._ensure_connected = MagicMock(return_value=True)

    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    store._conn.cursor = MagicMock(return_value=ctx)
    return store, cur


def _executed_sql(cur) -> list[str]:
    return [call.args[0] for call in cur.execute.call_args_list]


class TestOrphanedRestartFilter:
    """Aggregate queries on trades must exclude exit_reason='orphaned_restart'."""

    def test_fetch_closed_trades_pnl_filters_orphans(self):
        store, cur = _build_store(fetchone=(0.0, 0))
        store.fetch_closed_trades_pnl(since_date="2026-04-01", capital=100.0)
        sqls = _executed_sql(cur)
        assert len(sqls) == 2, "expected daily + weekly queries"
        for sql in sqls:
            assert "orphaned_restart" in sql, (
                f"DD reconcile query missing orphan filter — will miscount "
                f"synthetic closes into daily/weekly PnL.\nSQL: {sql}"
            )

    def test_fetch_recent_closed_trades_filters_orphans(self):
        store, cur = _build_store(fetchall=[])
        store.fetch_recent_closed_trades(limit=5)
        sqls = _executed_sql(cur)
        assert len(sqls) == 1
        assert "orphaned_restart" in sqls[0], (
            "Dashboard recent-trades feed must hide synthetic orphan closes."
        )

    def test_get_journal_summary_filters_orphans(self):
        store, cur = _build_store(fetchone=(0, 0, 0, 0.0, 0.0), fetchall=[])
        store.get_journal_summary(last_n_days=7)
        sqls = _executed_sql(cur)
        # Three aggregate queries on trades + one on trade_rejections.
        trade_sqls = [s for s in sqls if "FROM trades" in s]
        assert len(trade_sqls) == 3, (
            f"expected 3 trade-aggregation queries, got {len(trade_sqls)}"
        )
        for sql in trade_sqls:
            assert "orphaned_restart" in sql, (
                f"Journal summary query missing orphan filter — skews "
                f"win-rate/PnL shown on dashboard.\nSQL: {sql}"
            )


class TestValidOutcomesContract:
    """VALID_OUTCOMES is the authoritative whitelist of ml_setups.outcome_type
    values. Drift between emitters and this set triggers WARNING in
    update_ml_setup_outcome. These tests lock the contract."""

    def test_live_resolution_labels_present(self):
        """Labels emitted by execution_service/monitor.py outcome_map."""
        required = {
            "filled_tp", "filled_sl", "filled_trailing", "filled_timeout",
            "filled_guardian", "filled_slippage",
            "unfilled_timeout", "replaced", "filled_orphaned",
        }
        missing = required - VALID_OUTCOMES
        assert not missing, f"live-path labels missing from whitelist: {missing}"

    def test_shadow_labels_present(self):
        """Labels emitted by execution_service/shadow_monitor.py."""
        required = {
            "shadow_tp", "shadow_sl", "shadow_breakeven",
            "shadow_timeout", "shadow_no_fill", "shadow_orphaned",
        }
        missing = required - VALID_OUTCOMES
        assert not missing, f"shadow labels missing from whitelist: {missing}"

    def test_pre_execution_labels_present(self):
        """Labels emitted by main.py before the trade reaches the exchange."""
        required = {
            "data_blocked", "shadow_direction_filtered", "shadow_dedup",
            "trading_halted", "risk_rejected", "ai_rejected",
        }
        missing = required - VALID_OUTCOMES
        assert not missing, f"pre-exec labels missing from whitelist: {missing}"

    def test_outcome_orphan_row_returns_false(self):
        """UPDATE that affects 0 rows — setup_id has no matching ml_setups
        row (insert failed earlier or resolving a shadow never recorded).
        Must return False + log warning instead of silently succeeding."""
        from loguru import logger as loguru_logger

        store, cur = _build_store()
        cur.rowcount = 0  # simulate no row matched

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            ok = store.update_ml_setup_outcome(
                setup_id="nonexistent_id",
                outcome_type="filled_tp",
            )
        finally:
            loguru_logger.remove(sink_id)

        assert ok is False, "orphan update must not report success"
        joined = "\n".join(messages)
        assert "ML outcome orphan" in joined
        assert "nonexistent_id" in joined

    def test_non_market_outcomes_are_subset_of_valid(self):
        """Every label in NON_MARKET_OUTCOMES must be a known valid label."""
        unknown = NON_MARKET_OUTCOMES - VALID_OUTCOMES
        assert not unknown, (
            f"NON_MARKET_OUTCOMES contains labels not in VALID_OUTCOMES: {unknown}"
        )

    def test_market_filter_sql_excludes_all_non_market(self):
        """The helper must list every non-market label in sorted order so
        the generated SQL is stable across runs (useful for diffs/tests)."""
        sql = ml_market_outcome_filter_sql()
        assert sql.startswith("outcome_type NOT IN (")
        for label in NON_MARKET_OUTCOMES:
            assert f"'{label}'" in sql, f"Filter missing {label}"
        # Deterministic ordering (sorted)
        labels_in_sql = sql[len("outcome_type NOT IN ("):-1]
        emitted = [s.strip().strip("'") for s in labels_in_sql.split(",")]
        assert emitted == sorted(NON_MARKET_OUTCOMES)

    def test_market_filter_custom_column(self):
        sql = ml_market_outcome_filter_sql(column="t.outcome_type")
        assert sql.startswith("t.outcome_type NOT IN (")

    def test_unknown_outcome_logs_warning(self):
        """update_ml_setup_outcome must WARN (not raise) on drift.

        Project uses loguru — attach a temporary sink so we can capture the
        warning record without depending on stderr buffering.
        """
        from loguru import logger as loguru_logger

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            store, _ = _build_store()
            store.update_ml_setup_outcome(
                setup_id="abc",
                outcome_type="totally_made_up_label",
            )
        finally:
            loguru_logger.remove(sink_id)

        joined = "\n".join(messages)
        assert "ML outcome drift" in joined
        assert "totally_made_up_label" in joined
