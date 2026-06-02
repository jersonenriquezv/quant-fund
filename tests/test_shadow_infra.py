"""
Batch 0 infra tests:

- Shadow redis persistence across simulated restart
- risk_capital column consistency in shadow_mode rows

Uses in-memory fakes for RedisStore and PostgresStore to avoid requiring
live infra for unit scope. Separate DB-backed tests live in test_pnl_engine.py
under @pytest.mark.db.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from shared.ml_features import extract_risk_context


# ================================================================
# In-memory fakes
# ================================================================

class FakeRedis:
    """Minimal RedisStore stand-in that survives class-level reset."""

    def __init__(self):
        self._store: dict[str, tuple[str, float, int]] = {}
        self._client = object()  # ShadowMonitor._get_redis checks truthiness

    def set_bot_state(self, key: str, value: str, ttl: int = 86400) -> None:
        self._store[key] = (value, time.time(), ttl)

    def get_bot_state(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        value, saved_at, ttl = entry
        if ttl > 0 and (time.time() - saved_at) > ttl:
            return None
        return value

    def wipe(self):
        self._store.clear()


class FakePostgres:
    """Records calls; no SQL. Used by shadow_monitor persistence test."""

    def __init__(self):
        self.ml_rows: dict[str, dict] = {}
        self.orphan_cleanup_calls = 0
        self.metrics: list[tuple[str, float, dict | None]] = []

    def insert_metric(self, name: str, value: float = 1.0,
                      pair: str | None = None, labels: dict | None = None) -> None:
        self.metrics.append((name, value, labels))

    def update_ml_shadow_tracking(self, setup_id: str, fields: dict) -> None:
        self.ml_rows.setdefault(setup_id, {}).update(fields)

    def update_ml_setup_outcome(self, setup_id: str, **kwargs) -> None:
        self.ml_rows.setdefault(setup_id, {}).update(kwargs)

    def resolve_orphaned_shadow_setups(self, max_age_hours: float = 36.0) -> int:
        self.orphan_cleanup_calls += 1
        return 0


@dataclass
class FakeDataService:
    redis: FakeRedis
    postgres: FakePostgres


class FakeRiskState:
    def __init__(self, capital: float = 86.0):
        self._capital = capital

    def get_capital(self) -> float:
        return self._capital

    def get_open_positions_count(self) -> int:
        return 2

    def get_daily_dd_pct(self) -> float:
        return 0.03

    def get_weekly_dd_pct(self) -> float:
        return 0.05

    def get_trades_today_count(self) -> int:
        return 4


class FakeRiskService:
    def __init__(self, capital: float = 86.0):
        self._state = FakeRiskState(capital)


# ================================================================
# risk_capital consistency
# ================================================================

class TestRiskCapitalConsistency:
    """Regression: ml_setups.risk_capital must be aligned with the capital
    used for sizing. Live trades → live OKX balance. Shadow trades →
    SHADOW_CAPITAL. Previously both paths wrote live balance, causing
    confusing rows where shadow_margin=$500 but risk_capital=$86.
    """

    def test_live_path_uses_live_capital(self):
        risk = FakeRiskService(capital=86.30)
        ctx = extract_risk_context(risk)
        assert ctx["risk_capital"] == pytest.approx(86.30, abs=1e-9)

    def test_shadow_override_replaces_capital(self):
        risk = FakeRiskService(capital=86.30)
        ctx = extract_risk_context(risk, capital_override=500.0)
        assert ctx["risk_capital"] == pytest.approx(500.0, abs=1e-9)

    def test_shadow_override_does_not_mutate_risk_state(self):
        risk = FakeRiskService(capital=86.30)
        extract_risk_context(risk, capital_override=500.0)
        # Override should not leak into risk_service state
        assert risk._state.get_capital() == pytest.approx(86.30, abs=1e-9)

    def test_other_risk_fields_unchanged_by_override(self):
        risk = FakeRiskService(capital=86.30)
        live = extract_risk_context(risk)
        shadow = extract_risk_context(risk, capital_override=500.0)
        for field in ("risk_open_positions", "risk_daily_dd_pct",
                      "risk_weekly_dd_pct", "risk_trades_today"):
            assert live[field] == shadow[field], (
                f"Override must only affect risk_capital, not {field}"
            )

    def test_override_zero_is_respected(self):
        """Explicit override=0.0 must produce risk_capital=0, not fall back."""
        risk = FakeRiskService(capital=86.30)
        ctx = extract_risk_context(risk, capital_override=0.0)
        assert ctx["risk_capital"] == 0.0


# ================================================================
# Shadow redis persistence (docker-restart simulation)
# ================================================================

@pytest.fixture
def shadow_factory():
    """Returns a fresh (data_service, ShadowMonitor) pair per test.

    The redis instance is shared across invocations within the factory so
    we can simulate 'kill ShadowMonitor / restart' with state intact.
    """
    from execution_service.shadow_monitor import ShadowMonitor

    redis = FakeRedis()
    postgres = FakePostgres()
    data_service = FakeDataService(redis=redis, postgres=postgres)

    def make():
        # Restore is deferred (Redis isn't connected at __init__ in prod —
        # see ShadowMonitor._ensure_restored). The factory simulates "instance
        # up and first candle tick processed" by triggering the restore once,
        # so restore-logic tests stay focused. Deferral itself is covered by
        # TestShadowRestoreDeferral below.
        mon = ShadowMonitor(data_service=data_service, notifier=None)
        mon._ensure_restored()
        return mon

    return redis, postgres, make


def _mk_setup(setup_id: str = "s1", pair: str = "BTC/USDT",
              direction: str = "long", setup_type: str = "setup_f",
              entry: float = 75000.0):
    """Minimal TradeSetup stand-in matching shared.models.TradeSetup fields."""
    from shared.models import TradeSetup
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair,
        direction=direction,
        setup_type=setup_type,
        entry_price=entry,
        sl_price=entry * 0.99 if direction == "long" else entry * 1.01,
        tp1_price=entry * 1.005 if direction == "long" else entry * 0.995,
        tp2_price=entry * 1.02 if direction == "long" else entry * 0.98,
        confluences=["order_block_15m"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
        setup_id=setup_id,
    )


class _FakeApproval:
    approved = True
    position_size = 0.001
    leverage = 5.0
    reason = "test"


class TestShadowRedisPersistence:
    """Simulate docker restart: positions saved to redis by instance A
    must be restored by instance B with identical state.

    Catches the 43-orphans/7d bug where shadow positions were lost on
    restart, leaving DB rows with NULL outcome until the 6h orphan sweep.
    """

    def test_active_position_survives_restart(self, shadow_factory):
        redis, postgres, make = shadow_factory
        mon_a = make()
        setup = _mk_setup(setup_id="sp1", entry=75000.0)
        accepted = mon_a.add_shadow(setup, orderbook=None, risk_approval=_FakeApproval())
        assert accepted is True
        assert mon_a.active_count == 1

        # Drop instance A — redis still has the state
        del mon_a

        # Instance B spins up with same redis → should find the position
        mon_b = make()
        assert mon_b.active_count == 1, "restart lost active shadow position"

        # State fields preserved
        pos = list(mon_b._positions.values())[0]
        assert pos.setup_id == "sp1"
        assert pos.entry_price == pytest.approx(75000.0, abs=1e-9)
        assert pos.position_size == pytest.approx(0.001, abs=1e-9)
        assert pos.filled is False

    def test_filled_position_preserves_fill_state(self, shadow_factory):
        redis, postgres, make = shadow_factory
        mon_a = make()
        setup = _mk_setup(setup_id="sp2", entry=75000.0)
        mon_a.add_shadow(setup, orderbook=None, risk_approval=_FakeApproval())
        # Manually mark filled (simulate check_candle marking it filled)
        pos = mon_a._positions["sp2"]
        pos.filled = True
        pos.fill_time = time.time()
        pos.tp1_touched = True
        pos.sl_price = pos.entry_price  # BE SL move
        mon_a._save_to_redis()

        del mon_a
        mon_b = make()
        restored = mon_b._positions["sp2"]
        assert restored.filled is True
        assert restored.tp1_touched is True
        assert restored.sl_price == pytest.approx(pos.entry_price, abs=1e-9), (
            "breakeven SL move must persist across restart"
        )

    def test_expired_positions_skipped_on_load(self, shadow_factory):
        redis, postgres, make = shadow_factory
        mon_a = make()
        setup = _mk_setup(setup_id="expired", entry=75000.0)
        mon_a.add_shadow(setup, orderbook=None, risk_approval=_FakeApproval())
        # Force-age the detection_time beyond max shadow lifetime
        from config.settings import settings
        pos = mon_a._positions["expired"]
        max_age_s = (settings.SHADOW_ENTRY_TIMEOUT_HOURS + settings.SHADOW_TRADE_TIMEOUT_HOURS) * 3600
        pos.detection_time = time.time() - max_age_s - 60
        mon_a._save_to_redis()

        del mon_a
        mon_b = make()
        assert "expired" not in mon_b._positions, (
            "expired position must not be restored on restart"
        )

    def test_empty_redis_yields_empty_monitor(self, shadow_factory):
        redis, _postgres, make = shadow_factory
        redis.wipe()
        mon = make()
        assert mon.active_count == 0

    def test_save_every_state_transition(self, shadow_factory):
        """Each add/fill/tp1/resolve should persist to redis so crashes
        between transitions don't lose state.
        """
        redis, _postgres, make = shadow_factory
        mon = make()
        before = redis.get_bot_state("shadow_positions")
        setup = _mk_setup(setup_id="sp3", entry=75000.0)
        mon.add_shadow(setup, orderbook=None, risk_approval=_FakeApproval())
        after_add = redis.get_bot_state("shadow_positions")
        assert before != after_add
        assert "sp3" in after_add

    def test_orphan_cleanup_invoked_on_init(self, shadow_factory):
        """DB orphan sweep runs once on ShadowMonitor construction."""
        _redis, postgres, make = shadow_factory
        _mon = make()
        assert postgres.orphan_cleanup_calls == 1, (
            "ShadowMonitor.__init__ must trigger one orphan sweep to clean "
            "rows stranded by the previous crash."
        )


class TestShadowRestoreDeferral:
    """Orphan-leak root cause: ShadowMonitor is constructed BEFORE DataService
    connects Redis, so restore-in-__init__ always saw redis=None and silently
    skipped — every restart lost all in-flight shadows. Restore must be
    deferred until candles flow (Redis guaranteed up).
    """

    def test_restore_not_run_in_init(self, shadow_factory):
        from execution_service.shadow_monitor import ShadowMonitor
        redis, postgres, _make = shadow_factory
        # Seed redis with a saved position, then construct raw (no restore).
        seed = ShadowMonitor(data_service=FakeDataService(redis=redis, postgres=postgres),
                             notifier=None)
        seed._ensure_restored()
        seed.add_shadow(_mk_setup(setup_id="deferred", entry=75000.0),
                        orderbook=None, risk_approval=_FakeApproval())
        del seed

        postgres.orphan_cleanup_calls = 0
        data_service = FakeDataService(redis=redis, postgres=postgres)
        mon = ShadowMonitor(data_service=data_service, notifier=None)
        # __init__ must NOT have restored or swept.
        assert mon._restored is False
        assert mon.active_count == 0
        assert postgres.orphan_cleanup_calls == 0

        # First candle tick triggers the one-time restore.
        candle = SimpleNamespace(pair="ETH/USDT", timeframe="5m", timestamp=0,
                                 open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
        mon.check_candle("ETH/USDT", candle)
        assert mon._restored is True
        assert "deferred" in mon._positions, "first tick must restore saved positions"
        assert postgres.orphan_cleanup_calls == 1, "first tick must sweep orphans once"

        # Subsequent ticks do not re-restore.
        mon.check_candle("ETH/USDT", candle)
        assert postgres.orphan_cleanup_calls == 1


class TestShadowRestorePerRecordIsolation:
    """Orphan-leak root cause: one unparseable record in the Redis snapshot
    aborted the ENTIRE restore loop, dropping every in-flight shadow position
    (which then aged out as `shadow_orphaned`). Restore must isolate per
    record — a bad record is dropped, the rest survive.
    """

    def _corrupt_snapshot(self, redis):
        """Inject a record with an unknown field that ShadowPosition rejects,
        alongside whatever good records are already saved."""
        import json
        raw = redis.get_bot_state("shadow_positions")
        data = json.loads(raw) if raw else {}
        data["corrupt"] = {"setup_id": "corrupt", "not_a_field": 123}
        redis.set_bot_state("shadow_positions", json.dumps(data))

    def test_bad_record_does_not_abort_restore(self, shadow_factory):
        redis, _postgres, make = shadow_factory
        mon_a = make()
        mon_a.add_shadow(_mk_setup(setup_id="good", entry=75000.0),
                         orderbook=None, risk_approval=_FakeApproval())
        del mon_a

        self._corrupt_snapshot(redis)

        # Instance B: must still restore the good record despite the bad one.
        mon_b = make()
        assert "good" in mon_b._positions, (
            "a single bad record must not abort restore of valid positions"
        )
        assert "corrupt" not in mon_b._positions

    def test_bad_record_emits_dropped_metric(self, shadow_factory):
        redis, postgres, make = shadow_factory
        mon_a = make()
        mon_a.add_shadow(_mk_setup(setup_id="good2", entry=75000.0),
                         orderbook=None, risk_approval=_FakeApproval())
        del mon_a
        self._corrupt_snapshot(redis)
        postgres.metrics.clear()
        make()  # instance B restores
        dropped = [m for m in postgres.metrics
                   if m[0] == "shadow_redis_load_dropped"
                   and (m[2] or {}).get("reason") == "parse_error"]
        assert dropped, "parse failure must emit shadow_redis_load_dropped"
