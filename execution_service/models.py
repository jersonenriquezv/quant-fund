"""
Internal state models for Execution Service.

These are mutable — they track position lifecycle in memory.
NOT in shared/models.py because they're internal to this service.
"""

import uuid
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
    setup_id: str = ""          # ML tracking ID (from TradeSetup.setup_id)

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

    # Split entry tracking (swing setups with entry2_price > 0)
    is_split_entry: bool = False
    entry2_price: float = 0.0            # OB 75% level
    entry2_order_id: Optional[str] = None
    entry1_filled: bool = False
    entry2_filled: bool = False
    entry1_fill_price: float = 0.0
    entry2_fill_price: float = 0.0
    entry1_fill_size: float = 0.0
    entry2_fill_size: float = 0.0

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
    actual_exit_price: Optional[float] = None

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


@dataclass
class CampaignAdd:
    """A single pyramid add within a campaign."""
    add_number: int             # 1, 2, or 3
    margin: float               # USDT margin for this add
    size: float = 0.0           # Base currency size (filled)
    entry_price: float = 0.0    # Intended entry price
    actual_entry_price: float = 0.0  # Actual fill price
    filled: bool = False
    order_id: Optional[str] = None
    setup_type: str = ""        # Setup type that triggered this add
    placed_at: int = 0
    filled_at: Optional[int] = None


@dataclass
class PositionCampaign:
    """HTF position trade with pyramid adds and trailing SL.

    State machine:
        pending_initial → active → closed
        pending_initial → closed  (entry timeout or cancel)
        active (pending_add) → active  (add filled or timed out)
        active → closed  (trailing SL hit, timeout, emergency)

    No TP orders — campaigns exit via trailing SL only.
    """

    # Identity
    pair: str
    direction: str              # "long" or "short"
    campaign_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # Phase: pending_initial, active, closed
    phase: str = "pending_initial"

    # Initial entry
    initial_entry_price: float = 0.0
    initial_sl_price: float = 0.0
    initial_order_id: Optional[str] = None
    initial_size: float = 0.0        # Base currency (filled)
    initial_setup_type: str = ""
    initial_margin: float = 0.0

    # Actual fill
    actual_initial_entry: Optional[float] = None

    # Campaign SL — one stop-market covering total position
    current_sl_price: float = 0.0
    sl_order_id: Optional[str] = None

    # Cumulative position tracking
    total_size: float = 0.0          # Sum of all filled sizes
    total_margin: float = 0.0        # Sum of all margins
    weighted_entry: float = 0.0      # VWAP of all fills

    # Pyramid adds
    adds: list = field(default_factory=list)  # list[CampaignAdd]
    pending_add: Optional[CampaignAdd] = None

    # AI / context
    ai_confidence: float = 0.0
    htf_bias: str = ""

    # Timestamps
    created_at: int = 0
    filled_at: Optional[int] = None
    closed_at: Optional[int] = None

    # Exit
    close_reason: Optional[str] = None
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0

    # SL fetch failure tracking
    sl_fetch_failures: int = 0
    # Emergency retry tracking
    emergency_retries: int = 0

    # Database tracking
    db_campaign_id: Optional[int] = None

    # Leverage
    leverage: float = 1.0

    def update_weighted_entry(self) -> None:
        """Recalculate VWAP from initial fill + all filled adds."""
        total_notional = 0.0
        total_size = 0.0

        if self.actual_initial_entry and self.initial_size > 0:
            total_notional += self.actual_initial_entry * self.initial_size
            total_size += self.initial_size

        for add in self.adds:
            if add.filled and add.actual_entry_price > 0 and add.size > 0:
                total_notional += add.actual_entry_price * add.size
                total_size += add.size

        self.total_size = total_size
        if total_size > 0:
            self.weighted_entry = total_notional / total_size

    def get_add_margin(self, add_number: int) -> float:
        """Return the margin for a given add number (1-indexed)."""
        from config.settings import settings
        margins = {
            1: settings.HTF_ADD1_MARGIN,
            2: settings.HTF_ADD2_MARGIN,
            3: settings.HTF_ADD3_MARGIN,
        }
        return margins.get(add_number, 0.0)

    def current_rr(self) -> float:
        """Calculate current R:R from weighted entry vs current SL distance and profit."""
        if not self.weighted_entry or not self.initial_sl_price:
            return 0.0
        risk = abs(self.weighted_entry - self.initial_sl_price)
        if risk <= 0:
            return 0.0
        # Use initial entry to measure unrealized progress
        if self.direction == "long":
            reward = self.weighted_entry - self.initial_sl_price
        else:
            reward = self.initial_sl_price - self.weighted_entry
        return reward / risk if risk > 0 else 0.0
