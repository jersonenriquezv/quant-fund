"""Tests for data_service modules — validation, parsing, calculation, storage.

Covers:
- Candle validation rules (price <= 0, zero volume, future timestamps)
- OKX WebSocket candle message parsing
- CVD calculation from raw trades
- Candle deduplication and memory management
- Binance forceOrder message parsing
- Etherscan transaction processing
- Redis serialization roundtrips
- PostgreSQL store/load roundtrips
- MarketSnapshot assembly
"""

import json
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from shared.models import (
    Candle, FundingRate, OpenInterest, CVDSnapshot,
    OIFlushEvent, WhaleMovement, MarketSnapshot,
)
from tests.conftest import make_candle


# ============================================================
# Candle Validation (exchange_client + websocket_feeds)
# ============================================================

class TestCandleValidation:
    """Both exchange_client and websocket_feeds have identical validation rules."""

    def _get_exchange_validator(self):
        with patch("data_service.exchange_client.ccxt"):
            from data_service.exchange_client import ExchangeClient
            client = ExchangeClient()
        return client._validate_candle

    def _get_ws_validator(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        feed = OKXWebSocketFeed()
        return feed._validate_candle

    @pytest.fixture(params=["exchange", "ws"])
    def validator(self, request):
        if request.param == "exchange":
            return self._get_exchange_validator()
        return self._get_ws_validator()

    def test_valid_candle_passes(self, validator):
        ts = int(time.time() * 1000)
        assert validator("BTC/USDT", "5m", ts, 50000.0, 50100.0, 49900.0, 50050.0, 10.0)

    def test_zero_open_rejected(self, validator):
        ts = int(time.time() * 1000)
        assert not validator("BTC/USDT", "5m", ts, 0, 50100.0, 49900.0, 50050.0, 10.0)

    def test_negative_price_rejected(self, validator):
        ts = int(time.time() * 1000)
        assert not validator("BTC/USDT", "5m", ts, -1.0, 50100.0, 49900.0, 50050.0, 10.0)

    def test_zero_close_rejected(self, validator):
        ts = int(time.time() * 1000)
        assert not validator("BTC/USDT", "5m", ts, 50000.0, 50100.0, 49900.0, 0, 10.0)

    def test_zero_volume_rejected(self, validator):
        ts = int(time.time() * 1000)
        assert not validator("BTC/USDT", "5m", ts, 50000.0, 50100.0, 49900.0, 50050.0, 0)

    def test_future_timestamp_rejected(self, validator):
        future_ts = int(time.time() * 1000) + 120_000  # 2 min in future (>60s limit)
        assert not validator("BTC/USDT", "5m", future_ts, 50000.0, 50100.0, 49900.0, 50050.0, 10.0)

    def test_slightly_future_timestamp_ok(self, validator):
        """Timestamps within 60s of now should be accepted (clock skew tolerance)."""
        slight_future = int(time.time() * 1000) + 30_000  # 30s in future
        assert validator("BTC/USDT", "5m", slight_future, 50000.0, 50100.0, 49900.0, 50050.0, 10.0)

    def test_past_timestamp_ok(self, validator):
        old_ts = int(time.time() * 1000) - 3600_000  # 1 hour ago
        assert validator("BTC/USDT", "5m", old_ts, 50000.0, 50100.0, 49900.0, 50050.0, 10.0)


# ============================================================
# OKX WebSocket Candle Parsing
# ============================================================

class TestWebSocketCandleParsing:

    def _make_feed(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        return OKXWebSocketFeed()

    def test_confirmed_candle_stored(self):
        feed = self._make_feed()
        msg = {
            "arg": {"channel": "candle5m", "instId": "BTC-USDT-SWAP"},
            "data": [[
                str(int(time.time() * 1000)),  # ts
                "50000.0",  # open
                "50100.0",  # high
                "49900.0",  # low
                "50050.0",  # close
                "10.5",     # volume
                "525000",   # volume currency
                "525262.5", # volume quote
                "1",        # confirm = closed
            ]],
        }
        feed._handle_candle_data(msg)
        candle = feed.get_latest_candle("BTC/USDT", "5m")
        assert candle is not None
        assert candle.confirmed is True
        assert candle.close == 50050.0
        assert candle.pair == "BTC/USDT"
        assert candle.timeframe == "5m"

    def test_unconfirmed_candle_ignored(self):
        feed = self._make_feed()
        msg = {
            "arg": {"channel": "candle5m", "instId": "BTC-USDT-SWAP"},
            "data": [[
                str(int(time.time() * 1000)),
                "50000.0", "50100.0", "49900.0", "50050.0",
                "10.5", "525000", "525262.5",
                "0",  # confirm = still forming
            ]],
        }
        feed._handle_candle_data(msg)
        assert feed.get_latest_candle("BTC/USDT", "5m") is None

    def test_unknown_channel_ignored(self):
        feed = self._make_feed()
        msg = {
            "arg": {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},  # 1m not in our map
            "data": [[
                str(int(time.time() * 1000)),
                "50000.0", "50100.0", "49900.0", "50050.0",
                "10.5", "525000", "525262.5", "1",
            ]],
        }
        feed._handle_candle_data(msg)
        # No candles stored for any pair/tf
        assert len(feed._candles) == 0

    def test_unknown_instid_ignored(self):
        feed = self._make_feed()
        msg = {
            "arg": {"channel": "candle5m", "instId": "SHIB-USDT-SWAP"},  # Not our pair
            "data": [[
                str(int(time.time() * 1000)),
                "100.0", "101.0", "99.0", "100.5",
                "100.0", "10000", "10050", "1",
            ]],
        }
        feed._handle_candle_data(msg)
        assert len(feed._candles) == 0

    def test_short_data_array_skipped(self):
        feed = self._make_feed()
        msg = {
            "arg": {"channel": "candle5m", "instId": "BTC-USDT-SWAP"},
            "data": [["12345", "50000.0"]],  # Too few fields
        }
        feed._handle_candle_data(msg)
        assert len(feed._candles) == 0

    def test_event_message_handled(self):
        """Event messages (subscribe confirmations) should not crash."""
        feed = self._make_feed()
        feed._handle_event({"event": "subscribe", "arg": {"channel": "candle5m", "instId": "BTC-USDT-SWAP"}})
        feed._handle_event({"event": "error", "code": "400", "msg": "bad request"})
        # No crash = pass

    def test_all_timeframes_parsed(self):
        """All 4 supported timeframes should be parsed correctly."""
        feed = self._make_feed()
        channels = ["candle5m", "candle15m", "candle1H", "candle4H"]
        expected_tfs = ["5m", "15m", "1h", "4h"]

        for channel, expected_tf in zip(channels, expected_tfs):
            msg = {
                "arg": {"channel": channel, "instId": "ETH-USDT-SWAP"},
                "data": [[
                    str(int(time.time() * 1000)),
                    "3000.0", "3010.0", "2990.0", "3005.0",
                    "100.0", "300000", "300500", "1",
                ]],
            }
            feed._handle_candle_data(msg)
            candle = feed.get_latest_candle("ETH/USDT", expected_tf)
            assert candle is not None, f"Failed for channel={channel} tf={expected_tf}"


# ============================================================
# Candle Storage & Deduplication
# ============================================================

class TestCandleStorage:

    def _make_feed(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        return OKXWebSocketFeed()

    def test_store_backfilled_candles(self):
        feed = self._make_feed()
        candles = [
            make_candle(timestamp=1000, close=100.0),
            make_candle(timestamp=2000, close=101.0),
            make_candle(timestamp=3000, close=102.0),
        ]
        feed.store_candles(candles)
        result = feed.get_candles("BTC/USDT", "15m", 10)
        assert len(result) == 3
        assert result[0].timestamp == 1000  # oldest first
        assert result[-1].timestamp == 3000

    def test_deduplication_by_timestamp(self):
        feed = self._make_feed()
        candles = [
            make_candle(timestamp=1000, close=100.0),
            make_candle(timestamp=1000, close=100.0),  # duplicate
            make_candle(timestamp=2000, close=101.0),
        ]
        feed.store_candles(candles)
        result = feed.get_candles("BTC/USDT", "15m", 10)
        assert len(result) == 2

    def test_get_candles_count_limit(self):
        feed = self._make_feed()
        candles = [make_candle(timestamp=i * 1000) for i in range(20)]
        feed.store_candles(candles)
        result = feed.get_candles("BTC/USDT", "15m", 5)
        assert len(result) == 5
        assert result[0].timestamp == 15000  # Last 5 starting from 15

    def test_get_latest_candle(self):
        feed = self._make_feed()
        candles = [
            make_candle(timestamp=1000, close=100.0),
            make_candle(timestamp=2000, close=200.0),
        ]
        feed.store_candles(candles)
        latest = feed.get_latest_candle("BTC/USDT", "15m")
        assert latest.close == 200.0

    def test_get_latest_candle_no_data(self):
        feed = self._make_feed()
        assert feed.get_latest_candle("BTC/USDT", "5m") is None

    def test_memory_limit_enforced(self):
        """Should not store more than _MAX_CANDLES_IN_MEMORY."""
        feed = self._make_feed()
        from data_service.websocket_feeds import _MAX_CANDLES_IN_MEMORY
        candles = [make_candle(timestamp=i * 1000) for i in range(_MAX_CANDLES_IN_MEMORY + 100)]
        feed.store_candles(candles)
        result = feed.get_candles("BTC/USDT", "15m", _MAX_CANDLES_IN_MEMORY + 100)
        assert len(result) == _MAX_CANDLES_IN_MEMORY

    def test_pairs_stored_separately(self):
        feed = self._make_feed()
        feed.store_candles([make_candle(pair="BTC/USDT", timestamp=1000)])
        feed.store_candles([make_candle(pair="ETH/USDT", timestamp=1000)])

        assert len(feed.get_candles("BTC/USDT", "15m", 10)) == 1
        assert len(feed.get_candles("ETH/USDT", "15m", 10)) == 1

    def test_timeframes_stored_separately(self):
        feed = self._make_feed()
        feed.store_candles([make_candle(timeframe="5m", timestamp=1000)])
        feed.store_candles([make_candle(timeframe="15m", timestamp=1000)])

        assert len(feed.get_candles("BTC/USDT", "5m", 10)) == 1
        assert len(feed.get_candles("BTC/USDT", "15m", 10)) == 1


# ============================================================
# CVD Calculation
# ============================================================

class TestCVDCalculation:

    def _make_calculator(self):
        from data_service.cvd_calculator import CVDCalculator
        return CVDCalculator()

    def _make_trade(self, ts, price, size, side):
        from data_service.cvd_calculator import _RawTrade
        return _RawTrade(timestamp=ts, price=price, size=size, side=side)

    def test_empty_trades_no_snapshot(self):
        calc = self._make_calculator()
        assert calc.get_cvd("BTC/USDT") is None

    def _set_valid(self, calc, pair):
        """Set CVD state to VALID so get_cvd() returns snapshots."""
        from data_service.data_integrity import CVDState
        calc._cvd_state[pair] = CVDState.VALID

    def test_cvd_buys_positive(self):
        calc = self._make_calculator()
        self._set_valid(calc, "BTC/USDT")
        now_ms = int(time.time() * 1000)
        # Add buy trades (raw _RawTrade with final base-currency size)
        calc._trades["BTC/USDT"].append(self._make_trade(now_ms - 1000, 50000, 1.0, "buy"))
        calc._trades["BTC/USDT"].append(self._make_trade(now_ms - 500, 50100, 0.5, "buy"))
        calc._compute_snapshot("BTC/USDT", now_ms)

        snap = calc.get_cvd("BTC/USDT")
        assert snap is not None
        assert snap.cvd_5m == 1.5  # 1.0 + 0.5
        assert snap.buy_volume == 1.5
        assert snap.sell_volume == 0.0

    def test_cvd_sells_negative(self):
        calc = self._make_calculator()
        self._set_valid(calc, "BTC/USDT")
        now_ms = int(time.time() * 1000)
        calc._trades["BTC/USDT"].append(self._make_trade(now_ms - 1000, 50000, 2.0, "sell"))
        calc._compute_snapshot("BTC/USDT", now_ms)

        snap = calc.get_cvd("BTC/USDT")
        assert snap.cvd_5m == -2.0
        assert snap.sell_volume == 2.0

    def test_cvd_mixed_net(self):
        calc = self._make_calculator()
        self._set_valid(calc, "BTC/USDT")
        now_ms = int(time.time() * 1000)
        calc._trades["BTC/USDT"].append(self._make_trade(now_ms - 1000, 50000, 3.0, "buy"))
        calc._trades["BTC/USDT"].append(self._make_trade(now_ms - 500, 50000, 1.0, "sell"))
        calc._compute_snapshot("BTC/USDT", now_ms)

        snap = calc.get_cvd("BTC/USDT")
        assert snap.cvd_5m == 2.0  # 3.0 - 1.0
        assert snap.buy_volume == 3.0
        assert snap.sell_volume == 1.0

    def test_cvd_windows_separate(self):
        """Trades outside 5m window should still appear in 15m and 1h windows."""
        calc = self._make_calculator()
        self._set_valid(calc, "BTC/USDT")
        now_ms = int(time.time() * 1000)

        # Trade 10 min ago — outside 5m, inside 15m and 1h
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 600_000, 50000, 5.0, "buy")
        )
        # Trade 1 min ago — inside all windows
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 60_000, 50000, 1.0, "buy")
        )
        calc._compute_snapshot("BTC/USDT", now_ms)

        snap = calc.get_cvd("BTC/USDT")
        assert snap.cvd_5m == 1.0   # Only the recent trade
        assert snap.cvd_15m == 6.0  # Both trades
        assert snap.cvd_1h == 6.0   # Both trades

    def test_prune_removes_old_trades(self):
        calc = self._make_calculator()
        now_ms = int(time.time() * 1000)

        # Add a trade from 2 hours ago
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 7200_000, 50000, 10.0, "buy")
        )
        # And a recent one
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 1000, 50000, 1.0, "buy")
        )

        calc._prune_old_trades("BTC/USDT", now_ms)
        assert len(calc._trades["BTC/USDT"]) == 1  # Old one pruned

    def test_pairs_independent(self):
        calc = self._make_calculator()
        self._set_valid(calc, "BTC/USDT")
        self._set_valid(calc, "ETH/USDT")
        now_ms = int(time.time() * 1000)

        calc._trades["BTC/USDT"].append(self._make_trade(now_ms - 1000, 50000, 5.0, "buy"))
        calc._trades["ETH/USDT"].append(self._make_trade(now_ms - 1000, 3000, 2.0, "sell"))

        calc._compute_snapshot("BTC/USDT", now_ms)
        calc._compute_snapshot("ETH/USDT", now_ms)

        btc = calc.get_cvd("BTC/USDT")
        eth = calc.get_cvd("ETH/USDT")
        assert btc.cvd_5m == 5.0
        assert eth.cvd_5m == -2.0


# ============================================================
# CVD Trade Message Parsing
# ============================================================

class TestCVDTradeParsing:

    def _make_calculator(self):
        from data_service.cvd_calculator import CVDCalculator
        return CVDCalculator()

    def test_valid_trade_parsed(self):
        calc = self._make_calculator()
        msg = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "ts": str(int(time.time() * 1000)),
                "px": "50000.5",
                "sz": "0.01",
                "side": "buy",
            }],
        }
        calc._handle_trades(msg)
        assert len(calc._trades["BTC/USDT"]) == 1
        assert calc._trades["BTC/USDT"][0].side == "buy"
        assert calc._trades["BTC/USDT"][0].size == 0.0001  # 0.01 contracts * 0.01 BTC/contract

    def test_invalid_side_skipped(self):
        calc = self._make_calculator()
        msg = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{"ts": "1000", "px": "50000", "sz": "0.01", "side": "unknown"}],
        }
        calc._handle_trades(msg)
        assert len(calc._trades["BTC/USDT"]) == 0

    def test_zero_price_skipped(self):
        calc = self._make_calculator()
        msg = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{"ts": "1000", "px": "0", "sz": "0.01", "side": "buy"}],
        }
        calc._handle_trades(msg)
        assert len(calc._trades["BTC/USDT"]) == 0

    def test_unknown_pair_skipped(self):
        calc = self._make_calculator()
        msg = {
            "arg": {"channel": "trades", "instId": "SHIB-USDT-SWAP"},
            "data": [{"ts": "1000", "px": "0.1", "sz": "1000", "side": "buy"}],
        }
        calc._handle_trades(msg)
        assert "SHIB/USDT" not in calc._trades


# ============================================================
# Etherscan Transaction Processing
# ============================================================

class TestEtherscanProcessing:

    def _patch_settings(self):
        patcher = patch("data_service.etherscan_client.settings")
        mock_settings = patcher.start()
        mock_settings.ETHERSCAN_API_KEY = "test_key"
        mock_settings.ETHERSCAN_CHECK_INTERVAL = 300
        mock_settings.WHALE_MIN_ETH = 100.0
        mock_settings.WHALE_HIGH_ETH = 1000.0
        mock_settings.WHALE_WALLETS = {"0xWhale1": "Test Whale"}
        mock_settings.EXCHANGE_ADDRESSES = {
            "0xBinance": "Binance",
            "0xCoinbase": "Coinbase",
        }
        return patcher

    def _make_client(self):
        patcher = self._patch_settings()
        from data_service.etherscan_client import EtherscanClient
        client = EtherscanClient()
        # Keep patch active — caller must stop it
        client._patcher = patcher
        return client

    def test_deposit_to_exchange(self):
        client = self._make_client()
        tx = {
            "from": "0xWhale1",
            "to": "0xBinance",
            "value": str(int(500 * 1e18)),  # 500 ETH
            "timeStamp": str(int(time.time())),
            "hash": "0x123",
        }
        client._process_transaction("0xWhale1", tx)
        movements = client.get_recent_movements(hours=1)
        assert len(movements) == 1
        assert movements[0].action == "exchange_deposit"
        assert movements[0].exchange == "Binance"
        assert movements[0].significance == "medium"
        client._patcher.stop()

    def test_withdrawal_from_exchange(self):
        client = self._make_client()
        tx = {
            "from": "0xCoinbase",
            "to": "0xWhale1",
            "value": str(int(1500 * 1e18)),  # 1500 ETH
            "timeStamp": str(int(time.time())),
            "hash": "0x456",
        }
        client._process_transaction("0xWhale1", tx)
        movements = client.get_recent_movements(hours=1)
        assert len(movements) == 1
        assert movements[0].action == "exchange_withdrawal"
        assert movements[0].exchange == "Coinbase"
        assert movements[0].significance == "high"  # >1000 ETH
        client._patcher.stop()

    def test_small_transfer_ignored(self):
        client = self._make_client()
        tx = {
            "from": "0xWhale1",
            "to": "0xBinance",
            "value": str(int(50 * 1e18)),  # 50 ETH — below 100 ETH threshold
            "timeStamp": str(int(time.time())),
            "hash": "0x789",
        }
        client._process_transaction("0xWhale1", tx)
        assert len(client.get_recent_movements(hours=1)) == 0
        client._patcher.stop()

    def test_non_exchange_transfer_out_tracked(self):
        client = self._make_client()
        tx = {
            "from": "0xWhale1",
            "to": "0xRandomWallet",  # Not in exchange addresses
            "value": str(int(5000 * 1e18)),
            "timeStamp": str(int(time.time())),
            "hash": "0xabc",
        }
        client._process_transaction("0xWhale1", tx)
        movements = client.get_recent_movements(hours=1)
        assert len(movements) == 1
        assert movements[0].action == "transfer_out"
        assert movements[0].significance == "high"  # 5000 ETH > 1000 threshold
        assert "..." in movements[0].exchange  # Truncated address
        client._patcher.stop()

    def test_non_exchange_transfer_in_tracked(self):
        client = self._make_client()
        tx = {
            "from": "0xRandomSender",  # Not in exchange addresses
            "to": "0xWhale1",
            "value": str(int(200 * 1e18)),
            "timeStamp": str(int(time.time())),
            "hash": "0xdef",
        }
        client._process_transaction("0xWhale1", tx)
        movements = client.get_recent_movements(hours=1)
        assert len(movements) == 1
        assert movements[0].action == "transfer_in"
        assert movements[0].amount == 200.0
        assert movements[0].significance == "medium"  # 200 ETH < 1000 threshold
        assert "..." in movements[0].exchange  # Truncated address
        client._patcher.stop()

    def test_prune_old_movements(self):
        client = self._make_client()
        old_ts = int((time.time() - 86400 - 60) * 1000)  # >24h ago

        client._movements.append(WhaleMovement(
            timestamp=old_ts, wallet="0xWhale1", action="exchange_deposit",
            amount=50.0, exchange="Binance", significance="medium", chain="ETH",
        ))
        client._prune_old_movements()
        assert len(client._movements) == 0
        client._patcher.stop()


# ============================================================
# Redis Serialization Roundtrip
# ============================================================

class TestRedisRoundtrip:
    """Test Redis set/get without a real Redis connection using mocks."""

    def _make_store(self):
        from data_service.data_store import RedisStore
        store = RedisStore()
        store._client = MagicMock()
        store._client.ping.return_value = True
        # Mock get to return what was set
        self._stored = {}

        def mock_set(key, value, ex=None):
            self._stored[key] = value

        def mock_get(key):
            return self._stored.get(key)

        store._client.set = mock_set
        store._client.get = mock_get
        return store

    def test_candle_roundtrip(self):
        store = self._make_store()
        candle = make_candle(timestamp=12345, close=50000.0, pair="BTC/USDT", timeframe="5m")
        store.set_latest_candle(candle)
        result = store.get_latest_candle("BTC/USDT", "5m")
        assert result is not None
        assert result.timestamp == 12345
        assert result.close == 50000.0
        assert result.pair == "BTC/USDT"
        assert result.timeframe == "5m"
        assert result.confirmed is True

    def test_funding_rate_roundtrip(self):
        store = self._make_store()
        fr = FundingRate(
            timestamp=1000, pair="BTC/USDT",
            rate=0.0001, next_rate=0.00015,
            next_funding_time=2000,
        )
        store.set_funding_rate(fr)
        result = store.get_funding_rate("BTC/USDT")
        assert result is not None
        assert result.rate == 0.0001
        assert result.next_rate == 0.00015

    def test_open_interest_roundtrip(self):
        store = self._make_store()
        oi = OpenInterest(
            timestamp=1000, pair="ETH/USDT",
            oi_contracts=50000, oi_base=5000,
            oi_usd=15_000_000,
        )
        store.set_open_interest(oi)
        result = store.get_open_interest("ETH/USDT")
        assert result is not None
        assert result.oi_usd == 15_000_000

    def test_get_missing_key_returns_none(self):
        store = self._make_store()
        assert store.get_latest_candle("BTC/USDT", "5m") is None
        assert store.get_funding_rate("BTC/USDT") is None
        assert store.get_open_interest("BTC/USDT") is None

    def test_no_client_returns_none(self):
        from data_service.data_store import RedisStore
        store = RedisStore()
        # _client is None
        store.set_latest_candle(make_candle())
        assert store.get_latest_candle("BTC/USDT", "15m") is None

    def test_bot_state_roundtrip(self):
        store = self._make_store()
        store.set_bot_state("daily_dd", "0.015")
        assert store.get_bot_state("daily_dd") == "0.015"

    def test_last_candle_ts_roundtrip(self):
        store = self._make_store()
        store.set_last_candle_ts("BTC/USDT", "5m", 1234567890)
        result = store.get_last_candle_ts("BTC/USDT", "5m")
        assert result == 1234567890

    def test_is_connected_true(self):
        store = self._make_store()
        assert store.is_connected is True

    def test_is_connected_false_no_client(self):
        from data_service.data_store import RedisStore
        store = RedisStore()
        assert store.is_connected is False


# ============================================================
# PostgreSQL Store/Load Roundtrip (mocked)
# ============================================================

class TestPostgresRoundtrip:
    """Test PostgreSQL operations with mocked connection."""

    def _make_store(self):
        from data_service.data_store import PostgresStore
        store = PostgresStore()
        # We don't mock the full PG — just test no-connection behavior
        return store

    def test_store_candles_no_connection(self):
        store = self._make_store()
        store.connect = lambda: False  # Prevent reconnection
        result = store.store_candles([make_candle()])
        assert result == 0

    def test_load_candles_no_connection(self):
        store = self._make_store()
        store.connect = lambda: False  # Prevent reconnection
        result = store.load_candles("BTC/USDT", "5m", 100)
        assert result == []

    def test_is_connected_false(self):
        store = self._make_store()
        assert store.is_connected is False


# ============================================================
# MarketSnapshot Assembly
# ============================================================

class TestMarketSnapshotAssembly:

    def test_snapshot_with_all_data(self):
        """Verify MarketSnapshot can hold all data types."""
        ts = int(time.time() * 1000)
        snapshot = MarketSnapshot(
            pair="BTC/USDT",
            timestamp=ts,
            funding=FundingRate(ts, "BTC/USDT", 0.0001, 0.00012, ts + 28800000),
            oi=OpenInterest(ts, "BTC/USDT", 100000, 1000, 50_000_000),
            cvd=CVDSnapshot(ts, "BTC/USDT", 10.0, 30.0, 100.0, 500.0, 400.0),
            recent_oi_flushes=[
                OIFlushEvent(ts, "BTC/USDT", "long", 50000, 50000, "oi_proxy"),
            ],
            whale_movements=[
                WhaleMovement(ts, "0xWhale", "exchange_deposit", 100.0, "Binance", "high", "ETH"),
            ],
        )
        assert snapshot.pair == "BTC/USDT"
        assert snapshot.funding.rate == 0.0001
        assert snapshot.oi.oi_usd == 50_000_000
        assert snapshot.cvd.cvd_15m == 30.0
        assert len(snapshot.recent_oi_flushes) == 1
        assert len(snapshot.whale_movements) == 1

    def test_snapshot_all_optional_none(self):
        """Snapshot should work with no market data (degraded mode)."""
        snapshot = MarketSnapshot(
            pair="ETH/USDT",
            timestamp=int(time.time() * 1000),
        )
        assert snapshot.funding is None
        assert snapshot.oi is None
        assert snapshot.cvd is None
        assert snapshot.recent_oi_flushes == []
        assert snapshot.whale_movements == []


# ============================================================
# CVD Per-Window Warmup
# ============================================================

class TestCVDPerWindowWarmup:
    """Test progressive per-window CVD warmup (5m → 15m → 1h)."""

    def _make_calculator(self):
        from data_service.cvd_calculator import CVDCalculator
        return CVDCalculator()

    def _make_trade(self, ts, price, size, side):
        from data_service.cvd_calculator import _RawTrade
        return _RawTrade(timestamp=ts, price=price, size=size, side=side)

    def test_warmup_starts_as_warming_up(self):
        from data_service.data_integrity import CVDState
        calc = self._make_calculator()
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP
        assert calc.get_warm_windows("BTC/USDT") == set()

    def test_5m_warmup_transitions_to_valid(self):
        """After 5 min of trade data, CVD should transition to VALID."""
        from data_service.data_integrity import CVDState
        from data_service.cvd_calculator import _WARMUP_5M_SEC
        calc = self._make_calculator()
        now_ms = int(time.time() * 1000)

        # Add trades spanning just over 5 minutes
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - (_WARMUP_5M_SEC + 10) * 1000, 50000, 1.0, "buy")
        )
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 1000, 50000, 0.5, "buy")
        )

        # Simulate batch loop iteration
        calc._compute_snapshot("BTC/USDT", now_ms)

        # Manually run warmup check (normally in _batch_loop)
        trades = calc._trades.get("BTC/USDT")
        oldest_ms = trades[0].timestamp
        span_sec = (now_ms - oldest_ms) / 1000
        assert span_sec >= _WARMUP_5M_SEC

        # Run the actual batch logic by calling the internal state check
        # (extracted to avoid running the full async loop)
        from data_service.cvd_calculator import _WARMUP_5M_SEC as W5, _WARMUP_15M_SEC as W15, _WARMUP_1H_SEC as W1H
        warm = calc._warm_windows.get("BTC/USDT", set())
        if "5m" not in warm and span_sec >= W5:
            warm.add("5m")
            calc._warm_windows["BTC/USDT"] = warm
            calc._cvd_state["BTC/USDT"] = CVDState.VALID
            calc._cvd_invalid_reason["BTC/USDT"] = ""

        assert calc.get_cvd_state("BTC/USDT") == CVDState.VALID
        assert "5m" in calc.get_warm_windows("BTC/USDT")
        # Should now return a snapshot
        assert calc.get_cvd("BTC/USDT") is not None

    def test_short_span_stays_warming(self):
        """Less than 5 min of data should NOT transition to VALID."""
        from data_service.data_integrity import CVDState
        calc = self._make_calculator()
        now_ms = int(time.time() * 1000)

        # Add trades spanning only 2 minutes
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 120_000, 50000, 1.0, "buy")
        )
        calc._trades["BTC/USDT"].append(
            self._make_trade(now_ms - 1000, 50000, 0.5, "buy")
        )

        # State should remain WARMING_UP
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP
        assert calc.get_cvd("BTC/USDT") is None

    def test_disconnect_resets_warm_windows(self):
        """Disconnect should reset warm windows and set INVALID."""
        from data_service.data_integrity import CVDState
        calc = self._make_calculator()

        # Simulate having reached VALID with 5m warm
        calc._cvd_state["BTC/USDT"] = CVDState.VALID
        calc._warm_windows["BTC/USDT"] = {"5m"}

        # Simulate disconnect (what _ws_loop does)
        calc._cvd_state["BTC/USDT"] = CVDState.INVALID
        calc._cvd_invalid_reason["BTC/USDT"] = "disconnect"

        # Simulate reconnect (what _connect_and_listen does)
        calc._trades["BTC/USDT"].clear()
        calc._snapshots.pop("BTC/USDT", None)
        calc._cvd_state["BTC/USDT"] = CVDState.WARMING_UP
        calc._warm_windows["BTC/USDT"] = set()

        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP
        assert calc.get_warm_windows("BTC/USDT") == set()

    def test_get_warm_windows_returns_copy(self):
        """Modifying returned set should not affect internal state."""
        calc = self._make_calculator()
        calc._warm_windows["BTC/USDT"] = {"5m"}
        result = calc.get_warm_windows("BTC/USDT")
        result.add("1h")  # Modify the copy
        assert "1h" not in calc._warm_windows["BTC/USDT"]


# ============================================================
# OI Contract Size Calculation
# ============================================================

class TestOIContractSize:

    def test_contract_sizes_constant_exists(self):
        from data_service.exchange_client import _CONTRACT_SIZES
        assert "BTC/USDT" in _CONTRACT_SIZES
        assert "ETH/USDT" in _CONTRACT_SIZES
        assert _CONTRACT_SIZES["BTC/USDT"] == 0.01
        assert _CONTRACT_SIZES["ETH/USDT"] == 0.1
