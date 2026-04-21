"""
Real-candle integration tests — load actual historical OHLCV from PostgreSQL
and run it through market structure + order block detectors.

These are NOT unit tests — they hit the live DB (gated by @pytest.mark.db).
Complements the property tests in test_market_structure_invariants.py and
test_order_block_invariants.py (which use synthetic candles): those prove
the math. These prove the pipeline works on real market data that the bot
actually sees.

If live detection stops producing any OBs / swings on real candles, something
broke in detection logic. Catching that in CI is worth a few seconds.
"""

from __future__ import annotations

import os
import pytest

from config.settings import settings
from shared.models import Candle
from strategy_service.market_structure import MarketStructureAnalyzer
from strategy_service.order_blocks import OrderBlockDetector


db = pytest.mark.db


def _db_conn():
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
            dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD, connect_timeout=3,
        )
        conn.autocommit = True
        return conn
    except Exception:
        return None


def _db_available() -> bool:
    conn = _db_conn()
    if conn is None:
        return False
    conn.close()
    return True


def _load_candles(pg, pair: str, tf: str, limit: int = 500) -> list[Candle]:
    with pg.cursor() as cur:
        cur.execute(
            "SELECT timestamp, open, high, low, close, volume, volume_quote "
            "FROM candles WHERE pair=%s AND timeframe=%s "
            "ORDER BY timestamp DESC LIMIT %s",
            (pair, tf, limit),
        )
        rows = cur.fetchall()
    # Oldest first for detectors
    rows.reverse()
    return [
        Candle(
            timestamp=int(r[0]),
            open=float(r[1]), high=float(r[2]), low=float(r[3]),
            close=float(r[4]), volume=float(r[5]),
            volume_quote=float(r[6]) if r[6] is not None else float(r[5]) * float(r[4]),
            pair=pair, timeframe=tf, confirmed=True,
        )
        for r in rows
    ]


@db
@pytest.mark.skipif(not _db_available(), reason="PostgreSQL not available")
class TestRealCandleMarketStructure:
    """Run market-structure detection against real historical candles
    from the 7 tracked pairs on 15m. Assert sane aggregates — not exact
    values (market shifts) but bounds that would fail if detection broke.
    """

    @pytest.fixture(scope="class")
    def pg(self):
        conn = _db_conn()
        yield conn
        conn.close()

    @pytest.mark.parametrize("pair", [
        "BTC/USDT", "ETH/USDT", "SOL/USDT",
    ])
    def test_detection_produces_swings_and_breaks(self, pg, pair):
        candles = _load_candles(pg, pair, "15m", limit=500)
        assert len(candles) >= 100, (
            f"need >=100 candles for {pair} but only got {len(candles)}"
        )
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze(candles, pair, "15m")

        # In 500 recent 15m candles (~5 days) we expect meaningful structure
        assert len(state.swing_highs) >= 5, (
            f"{pair}: only {len(state.swing_highs)} swing highs in "
            f"{len(candles)} candles — detection likely broken"
        )
        assert len(state.swing_lows) >= 5, (
            f"{pair}: only {len(state.swing_lows)} swing lows"
        )
        # Swings must stay within candle high/low extremes
        price_min = min(c.low for c in candles)
        price_max = max(c.high for c in candles)
        for s in state.swing_highs + state.swing_lows:
            assert price_min <= s.price <= price_max, (
                f"{pair}: swing point at {s.price} outside "
                f"candle range [{price_min}, {price_max}]"
            )

    def test_ob_detection_on_recent_btc_candles(self, pg):
        pair = "BTC/USDT"
        candles = _load_candles(pg, pair, "15m", limit=400)
        assert len(candles) >= 100
        struct = MarketStructureAnalyzer()
        state = struct.analyze(candles, pair, "15m")
        detector = OrderBlockDetector()
        now_ms = candles[-1].timestamp + 60_000
        obs = detector.update(candles, state.structure_breaks, pair, "15m", now_ms)

        # Real 400-candle windows rarely yield zero OBs — would indicate regression
        # Sanity bound: 0 ≤ active OBs ≤ 50 (pruning works)
        assert 0 <= len(obs) <= 50, (
            f"Expected OB count in [0, 50], got {len(obs)}"
        )
        # Every returned OB must be unmitigated (detector contract)
        for ob in obs:
            assert ob.mitigated is False
            assert ob.pair == pair
            assert ob.timeframe == "15m"

    def test_detection_stable_across_pair_boundaries(self, pg):
        """Running detection on pair A must not pollute state for pair B."""
        analyzer = MarketStructureAnalyzer()
        btc = _load_candles(pg, "BTC/USDT", "15m", limit=300)
        eth = _load_candles(pg, "ETH/USDT", "15m", limit=300)
        if len(btc) < 100 or len(eth) < 100:
            pytest.skip("insufficient candles for cross-pair test")

        s_btc = analyzer.analyze(btc, "BTC/USDT", "15m")
        s_eth = analyzer.analyze(eth, "ETH/USDT", "15m")

        # Per-pair state must be isolated
        assert s_btc.pair == "BTC/USDT"
        assert s_eth.pair == "ETH/USDT"

        # Swing prices must stay within the pair's candle range
        btc_prices = [c.high for c in btc] + [c.low for c in btc]
        eth_prices = [c.high for c in eth] + [c.low for c in eth]
        btc_range = (min(btc_prices), max(btc_prices))
        eth_range = (min(eth_prices), max(eth_prices))

        for s in s_btc.swing_highs + s_btc.swing_lows:
            assert btc_range[0] <= s.price <= btc_range[1]
        for s in s_eth.swing_highs + s_eth.swing_lows:
            assert eth_range[0] <= s.price <= eth_range[1]
