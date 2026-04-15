"""Shared exchange metadata for Data Service modules."""

from __future__ import annotations

from config.settings import settings


OKX_SWAP_INSTRUMENTS = {
    "BTC/USDT": "BTC-USDT-SWAP",
    "ETH/USDT": "ETH-USDT-SWAP",
    "SOL/USDT": "SOL-USDT-SWAP",
    "DOGE/USDT": "DOGE-USDT-SWAP",
    "XRP/USDT": "XRP-USDT-SWAP",
    "LINK/USDT": "LINK-USDT-SWAP",
    "AVAX/USDT": "AVAX-USDT-SWAP",
}


CONTRACT_SIZES = {
    "BTC/USDT": 0.01,
    "ETH/USDT": 0.1,
    "SOL/USDT": 1.0,
    "DOGE/USDT": 1000.0,
    "XRP/USDT": 100.0,
    "LINK/USDT": 1.0,
    "AVAX/USDT": 1.0,
}


def active_okx_instruments() -> dict[str, str]:
    """Return configured trading pairs mapped to OKX instrument IDs."""
    return {pair: OKX_SWAP_INSTRUMENTS[pair] for pair in settings.TRADING_PAIRS}


def assert_supported_trading_pairs() -> None:
    """Fail fast if settings reference a pair without exchange metadata."""
    missing_inst = [p for p in settings.TRADING_PAIRS if p not in OKX_SWAP_INSTRUMENTS]
    missing_contract = [p for p in settings.TRADING_PAIRS if p not in CONTRACT_SIZES]
    if missing_inst or missing_contract:
        details = []
        if missing_inst:
            details.append(f"missing OKX instrument: {missing_inst}")
        if missing_contract:
            details.append(f"missing contract size: {missing_contract}")
        raise ValueError("; ".join(details))
