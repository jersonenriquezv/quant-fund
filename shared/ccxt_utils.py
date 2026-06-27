"""Shared ccxt hardening helpers.

OKX intermittently returns one instrument with ``id=None`` in its markets
response. ccxt 4.5.40's ``set_markets`` sorts ``markets_by_id`` and raises
``TypeError: '<' not supported between instances of 'str' and 'NoneType'``,
which crashes ``load_markets`` and any call that triggers it (position sync,
contract-size lookup, REST backfill). Filter the malformed entry before ccxt
ever sees it.
"""

from shared.logger import logger


def harden_okx_markets(exchange):
    """Drop markets with ``id=None`` so ccxt keysort never chokes on them.

    Wraps the exchange instance's ``fetch_markets`` so the filtering happens
    inside ``load_markets`` (before ``set_markets``/``keysort``). Idempotent —
    safe to call once per exchange instance. Returns the same exchange.
    """
    if getattr(exchange, "_okx_markets_hardened", False):
        return exchange

    _orig_fetch_markets = exchange.fetch_markets

    def _filtered_fetch_markets(params={}):
        markets = _orig_fetch_markets(params)
        cleaned = [m for m in markets if m.get("id") is not None]
        dropped = len(markets) - len(cleaned)
        if dropped:
            logger.warning(
                f"harden_okx_markets: dropped {dropped} market(s) with id=None"
            )
        return cleaned

    exchange.fetch_markets = _filtered_fetch_markets
    exchange._okx_markets_hardened = True
    return exchange
