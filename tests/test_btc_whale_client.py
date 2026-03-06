"""Tests for data_service.btc_whale_client — BTC UTXO transaction processing."""

import time
import pytest
from unittest.mock import patch

from data_service.btc_whale_client import BtcWhaleClient


WHALE_WALLET = "bc1qwhale00000000000000000000000000000test"
WHALE_LABEL = "Test Whale"
EXCHANGE_ADDR = "bc1qexchange000000000000000000000000000test"
EXCHANGE_NAME = "TestExchange"
NON_EXCHANGE_ADDR = "bc1qrandom0000000000000000000000000000test"


@pytest.fixture
def client():
    """Create a BtcWhaleClient with test wallets and exchange addresses."""
    with patch("data_service.btc_whale_client.settings") as mock_settings:
        mock_settings.BTC_WHALE_WALLETS = {WHALE_WALLET: WHALE_LABEL}
        mock_settings.BTC_EXCHANGE_ADDRESSES = {EXCHANGE_ADDR: EXCHANGE_NAME}
        mock_settings.WHALE_MIN_BTC = 10.0
        mock_settings.WHALE_HIGH_BTC = 100.0
        mock_settings.MEMPOOL_CHECK_INTERVAL = 300
        yield BtcWhaleClient()


def _make_tx(
    inputs: list[tuple[str, int]],
    outputs: list[tuple[str, int]],
    block_time: int | None = None,
) -> dict:
    """Build a fake mempool.space transaction.

    Args:
        inputs: List of (address, satoshis) tuples for vin.
        outputs: List of (address, satoshis) tuples for vout.
        block_time: Unix timestamp (seconds) or None for unconfirmed.
    """
    vin = [
        {"prevout": {"scriptpubkey_address": addr, "value": sats}}
        for addr, sats in inputs
    ]
    vout = [
        {"scriptpubkey_address": addr, "value": sats}
        for addr, sats in outputs
    ]
    status = {"confirmed": block_time is not None}
    if block_time:
        status["block_time"] = block_time
    return {"txid": f"tx_{id(inputs)}", "vin": vin, "vout": vout, "status": status}


# ============================================================
# Exchange deposits (Case 1: whale → exchange)
# ============================================================

class TestExchangeDeposit:

    def test_deposit_detected(self, client):
        """Whale sends BTC to exchange → exchange_deposit movement."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],  # 50 BTC
            outputs=[(EXCHANGE_ADDR, 49_9000_0000), (WHALE_WALLET, 1000_0000)],  # change
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        movements = client.get_recent_movements()
        assert len(movements) == 1
        m = movements[0]
        assert m.action == "exchange_deposit"
        assert m.exchange == EXCHANGE_NAME
        assert m.chain == "BTC"
        assert m.amount == pytest.approx(49.9, rel=1e-2)

    def test_high_significance_threshold(self, client):
        """Deposit >= WHALE_HIGH_BTC should be 'high' significance."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 200_0000_0000)],  # 200 BTC
            outputs=[(EXCHANGE_ADDR, 200_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        m = client.get_recent_movements()[0]
        assert m.significance == "high"

    def test_medium_significance_below_high(self, client):
        """Deposit < WHALE_HIGH_BTC but >= WHALE_MIN_BTC → medium."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],
            outputs=[(EXCHANGE_ADDR, 50_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        m = client.get_recent_movements()[0]
        assert m.significance == "medium"


# ============================================================
# Exchange withdrawals (Case 2: exchange → whale)
# ============================================================

class TestExchangeWithdrawal:

    def test_withdrawal_detected(self, client):
        """Exchange sends BTC to whale → exchange_withdrawal movement."""
        tx = _make_tx(
            inputs=[(EXCHANGE_ADDR, 30_0000_0000)],
            outputs=[(WHALE_WALLET, 29_9000_0000), (EXCHANGE_ADDR, 1000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        movements = client.get_recent_movements()
        assert len(movements) == 1
        m = movements[0]
        assert m.action == "exchange_withdrawal"
        assert m.exchange == EXCHANGE_NAME
        assert m.amount == pytest.approx(29.9, rel=1e-2)


# ============================================================
# Non-exchange transfers (Case 1b and Case 3)
# ============================================================

class TestNonExchangeTransfers:

    def test_transfer_out_to_unknown(self, client):
        """Whale sends to non-exchange address → transfer_out."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 20_0000_0000)],
            outputs=[(NON_EXCHANGE_ADDR, 19_9000_0000), (WHALE_WALLET, 1000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        movements = client.get_recent_movements()
        assert len(movements) == 1
        m = movements[0]
        assert m.action == "transfer_out"
        assert "..." in m.exchange  # truncated address

    def test_transfer_in_from_unknown(self, client):
        """Non-exchange sends to whale → transfer_in."""
        tx = _make_tx(
            inputs=[(NON_EXCHANGE_ADDR, 15_0000_0000)],
            outputs=[(WHALE_WALLET, 15_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        movements = client.get_recent_movements()
        assert len(movements) == 1
        m = movements[0]
        assert m.action == "transfer_in"


# ============================================================
# Filtering and edge cases
# ============================================================

class TestFiltering:

    def test_small_transfer_ignored(self, client):
        """Transfer below WHALE_MIN_BTC (10 BTC) should be ignored."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 5_0000_0000)],  # 5 BTC
            outputs=[(EXCHANGE_ADDR, 5_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        assert len(client.get_recent_movements()) == 0

    def test_prune_old_movements(self, client):
        """Movements older than 24h should be pruned."""
        old_ts = int((time.time() - 90000) * 1000)  # 25 hours ago
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],
            outputs=[(EXCHANGE_ADDR, 50_0000_0000)],
            block_time=int(time.time() - 90000),
        )
        client._process_transaction(WHALE_WALLET, tx)

        # _process_transaction calls _prune_old_movements at the end
        assert len(client.get_recent_movements()) == 0


class TestUSDEnrichment:

    def test_price_provider_computes_usd(self):
        """With a price provider, movements should have amount_usd and market_price."""
        with patch("data_service.btc_whale_client.settings") as mock_settings:
            mock_settings.BTC_WHALE_WALLETS = {WHALE_WALLET: WHALE_LABEL}
            mock_settings.BTC_EXCHANGE_ADDRESSES = {EXCHANGE_ADDR: EXCHANGE_NAME}
            mock_settings.WHALE_MIN_BTC = 10.0
            mock_settings.WHALE_HIGH_BTC = 100.0
            mock_settings.MEMPOOL_CHECK_INTERVAL = 300
            client = BtcWhaleClient(price_provider=lambda: 70000.0)

        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],  # 50 BTC
            outputs=[(EXCHANGE_ADDR, 50_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        m = client.get_recent_movements()[0]
        assert m.market_price == 70000.0
        assert m.amount_usd == pytest.approx(50 * 70000.0, rel=1e-2)

    def test_no_price_provider_zero_usd(self, client):
        """Without a price provider, amount_usd and market_price should be 0."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],
            outputs=[(EXCHANGE_ADDR, 50_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        m = client.get_recent_movements()[0]
        assert m.market_price == 0.0
        assert m.amount_usd == 0.0


class TestSerialization:

    def test_serialize_includes_label(self, client):
        """serialize_movements() should include wallet_label."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],
            outputs=[(EXCHANGE_ADDR, 50_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        records = client.serialize_movements()
        assert len(records) == 1
        assert records[0]["label"] == WHALE_LABEL
        assert records[0]["chain"] == "BTC"

    def test_serialize_includes_usd_fields(self, client):
        """serialize_movements() should include amount_usd and market_price."""
        tx = _make_tx(
            inputs=[(WHALE_WALLET, 50_0000_0000)],
            outputs=[(EXCHANGE_ADDR, 50_0000_0000)],
            block_time=int(time.time()),
        )
        client._process_transaction(WHALE_WALLET, tx)

        records = client.serialize_movements()
        assert "amount_usd" in records[0]
        assert "market_price" in records[0]
