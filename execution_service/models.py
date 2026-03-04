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

    State machine:
        pending_entry → active → tp1_hit → tp2_hit → closed
        pending_entry → closed  (entry timeout or cancel)
        active → closed         (SL hit, timeout, emergency)
        tp1_hit → closed        (SL hit)
        tp2_hit → closed        (SL/TP3 hit)
    """

    # Identity
    pair: str
    direction: str              # "long" or "short"
    setup_type: str             # "setup_a" or "setup_b"

    # Phase
    phase: str = "pending_entry"  # pending_entry, active, tp1_hit, tp2_hit, closed

    # Target prices (from TradeSetup)
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp3_price: float = 0.0

    # Position sizing (from RiskApproval)
    total_size: float = 0.0     # Full position size in base currency
    leverage: float = 1.0

    # Order IDs on exchange
    entry_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    tp3_order_id: Optional[str] = None

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
    close_reason: Optional[str] = None  # "tp1", "tp2", "tp3", "sl", "timeout", "emergency", "cancelled"
    pnl_pct: float = 0.0

    # Database tracking
    db_trade_id: Optional[int] = None
