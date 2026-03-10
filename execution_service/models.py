"""
Internal state models for Execution Service.

These are mutable — they track position lifecycle in memory.
NOT in shared/models.py because they're internal to this service.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ManagedPosition:
    """Tracks the full lifecycle of an executed trade.

    Simplified state machine (Fase 1):
        pending_entry → active → closed
        pending_entry → closed  (entry timeout or cancel)
        active → closed         (SL hit, TP hit, timeout, emergency)

    Exit management:
        - SL (stop-market) at sl_price for 100% of position
        - Single TP (limit) at tp2_price (2:1 R:R) for 100% of position
        - When price crosses tp1_price (1:1 R:R), SL moves to breakeven
    """

    # Identity
    pair: str
    direction: str              # "long" or "short"
    setup_type: str             # "setup_a" or "setup_b"

    # Phase
    phase: str = "pending_entry"  # pending_entry, active, closed

    # Target prices (from TradeSetup)
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp1_price: float = 0.0       # Breakeven trigger level (1:1 R:R)
    tp2_price: float = 0.0       # TP order level (2:1 R:R)

    # Position sizing (from RiskApproval)
    total_size: float = 0.0     # Full position size in base currency
    leverage: float = 1.0

    # Order IDs on exchange
    entry_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None   # Single TP at tp2_price

    # Actual fills
    actual_entry_price: Optional[float] = None
    filled_size: float = 0.0    # How much of entry actually filled

    # AI confidence for trade logging
    ai_confidence: float = 0.0

    # Timestamps
    created_at: int = 0         # When entry order was placed
    filled_at: Optional[int] = None
    closed_at: Optional[int] = None

    # Exit info
    close_reason: Optional[str] = None  # "tp", "sl", "timeout", "emergency", "cancelled"
    pnl_pct: float = 0.0

    # Breakeven + trailing tracking
    breakeven_hit: bool = False   # True after SL moved to breakeven
    trailing_sl_moved: bool = False  # True after SL moved to tp1_price

    # Emergency close retry tracking
    emergency_retries: int = 0

    # SL order fetch failure tracking (for "algo order not found" fallback)
    sl_fetch_failures: int = 0
    # Track the current SL trigger price (updated on SL adjustments)
    current_sl_price: float = 0.0

    # Database tracking
    db_trade_id: Optional[int] = None
