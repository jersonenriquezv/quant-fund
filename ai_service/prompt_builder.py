"""
Prompt construction for Claude trade evaluation.

Builds structured system + user prompts from TradeSetup + MarketSnapshot.
The prompt quality determines decision quality — this is the intellectual core.
"""

from config.settings import settings
from shared.models import TradeSetup, MarketSnapshot

_SYSTEM_PROMPT = """You are a senior crypto trading analyst at a quantitative fund. Your job is to evaluate trade setups detected by an automated SMC (Smart Money Concepts) system and decide whether the current market context supports the trade.

You are a FILTER — you do NOT generate trades. The system has already detected a valid pattern. Your job is to evaluate whether macro context, sentiment, and market conditions support executing it NOW.

You must respond ONLY with valid JSON in this exact format:
{
    "confidence": <float 0.0-1.0>,
    "approved": <bool>,
    "reasoning": "<1-3 sentences explaining your decision>",
    "adjustments": {
        "sl_price": <float or null>,
        "tp2_price": <float or null>,
        "tp3_price": <float or null>
    },
    "warnings": ["<warning 1>", "<warning 2>"]
}

Decision guidelines:
- confidence >= 0.60 AND approved=true: Trade proceeds to risk check
- confidence < 0.60 OR approved=false: Trade is discarded
- You should approve 30-60% of setups. If you approve everything, you add no value.
- Be skeptical. In crypto, most setups fail. Your job is to filter the bad ones.

Factors to evaluate:
1. FUNDING RATE: Extreme positive = overcrowded longs (caution for longs). Extreme negative = overcrowded shorts (opportunity for longs).
2. OPEN INTEREST: OI rising + price rising = genuine trend. OI rising + price falling = distribution. OI falling = no new capital entering.
3. CVD (Cumulative Volume Delta): CVD aligned with trade direction = confirmation. CVD diverging = warning sign.
4. LIQUIDATIONS: Recent cascade in the direction of the trade = exhaustion risk. Cascade against the trade direction = fuel for the move.
5. WHALE MOVEMENTS: Large deposits to exchanges = potential selling pressure. Withdrawals = accumulation signal.
6. HTF CONFLUENCE: Does the higher timeframe structure support this trade direction?
7. SETUP QUALITY: How strong are the confluences? Is the order block fresh? Volume confirmation?

CRITICAL RULES:
- NEVER approve a trade just because the pattern is valid. Context matters more than pattern.
- If funding rate is extreme (>0.03% or <-0.03%), increase skepticism for trades in the crowded direction.
- If major liquidation cascade just happened in the trade direction, the move may be exhausted — reduce confidence.
- If CVD diverges from price, the move lacks conviction — reduce confidence.
- When in doubt, reject. Capital preservation > opportunity capture."""


class PromptBuilder:
    """Builds system and evaluation prompts for Claude."""

    def build_system_prompt(self) -> str:
        """Return the system prompt. Cached — does not change between evaluations."""
        return _SYSTEM_PROMPT

    def build_evaluation_prompt(
        self,
        setup: TradeSetup,
        snapshot: MarketSnapshot,
        candles_context: dict,
    ) -> str:
        """Build the user prompt with concrete market data.

        Args:
            setup: Detected trade setup from Strategy Service.
            snapshot: Current market data (funding, OI, CVD, liquidations, whales).
            candles_context: Dict with recent price changes per timeframe.
        """
        sections = [
            self._build_setup_section(setup),
            self._build_funding_section(snapshot),
            self._build_oi_section(snapshot),
            self._build_cvd_section(snapshot),
            self._build_liquidation_section(snapshot),
            self._build_whale_section(snapshot),
            self._build_price_context_section(candles_context),
        ]

        return "\n\n".join(sections) + "\n\nEvaluate this setup and respond with JSON only."

    def _build_setup_section(self, setup: TradeSetup) -> str:
        setup_names = {
            "setup_a": "Setup A (Sweep + CHoCH + OB)",
            "setup_b": "Setup B (BOS + FVG + OB)",
        }
        return (
            f"## Trade Setup\n"
            f"- Pair: {setup.pair}\n"
            f"- Direction: {setup.direction}\n"
            f"- Type: {setup_names.get(setup.setup_type, setup.setup_type)}\n"
            f"- Entry: {setup.entry_price}\n"
            f"- Stop Loss: {setup.sl_price}\n"
            f"- TP1: {setup.tp1_price} (50% close at 1:1)\n"
            f"- TP2: {setup.tp2_price} (30% close at 1:2)\n"
            f"- TP3: {setup.tp3_price} (20% trailing)\n"
            f"- HTF Bias: {setup.htf_bias}\n"
            f"- Confluences: {', '.join(setup.confluences)}\n"
            f"- OB Timeframe: {setup.ob_timeframe}"
        )

    def _build_funding_section(self, snapshot: MarketSnapshot) -> str:
        if snapshot.funding is None:
            return "## Funding Rate\nNot available"

        rate = snapshot.funding.rate
        pct = rate * 100
        interp = self._interpret_funding(rate)

        return (
            f"## Funding Rate\n"
            f"- Current: {rate} ({pct:.4f}%)\n"
            f"- Next estimated: {snapshot.funding.next_rate}\n"
            f"- Interpretation: {interp}"
        )

    def _interpret_funding(self, rate: float) -> str:
        threshold = settings.FUNDING_EXTREME_THRESHOLD
        if rate > threshold:
            return "EXTREME positive — overcrowded longs, caution for long trades"
        elif rate < -threshold:
            return "EXTREME negative — overcrowded shorts, opportunity for longs"
        elif rate > 0:
            return "Mildly positive — slight long bias, normal range"
        elif rate < 0:
            return "Mildly negative — slight short bias, normal range"
        return "Neutral"

    def _build_oi_section(self, snapshot: MarketSnapshot) -> str:
        if snapshot.oi is None:
            return "## Open Interest\nNot available"

        return (
            f"## Open Interest\n"
            f"- OI (USD): ${snapshot.oi.oi_usd:,.0f}\n"
            f"- OI (contracts): {snapshot.oi.oi_contracts:,.0f}\n"
            f"- OI (base): {snapshot.oi.oi_base:.4f}"
        )

    def _build_cvd_section(self, snapshot: MarketSnapshot) -> str:
        if snapshot.cvd is None:
            return "## CVD (Cumulative Volume Delta)\nNot available"

        cvd = snapshot.cvd
        buy_pct = (cvd.buy_volume / (cvd.buy_volume + cvd.sell_volume) * 100
                   if (cvd.buy_volume + cvd.sell_volume) > 0 else 50)

        return (
            f"## CVD (Cumulative Volume Delta)\n"
            f"- 5min: {cvd.cvd_5m:+.2f}\n"
            f"- 15min: {cvd.cvd_15m:+.2f}\n"
            f"- 1h: {cvd.cvd_1h:+.2f}\n"
            f"- Buy volume (1h): {cvd.buy_volume:.2f}\n"
            f"- Sell volume (1h): {cvd.sell_volume:.2f}\n"
            f"- Buy dominance: {buy_pct:.1f}%"
        )

    def _build_liquidation_section(self, snapshot: MarketSnapshot) -> str:
        liqs = snapshot.recent_liquidations
        if not liqs:
            return "## Recent Liquidations\nNo recent liquidations"

        total = sum(l.size_usd for l in liqs)
        long_usd = sum(l.size_usd for l in liqs if l.side == "long")
        short_usd = sum(l.size_usd for l in liqs if l.side == "short")

        return (
            f"## Recent Liquidations\n"
            f"- Total: ${total:,.0f}\n"
            f"- Long liquidations: ${long_usd:,.0f}\n"
            f"- Short liquidations: ${short_usd:,.0f}\n"
            f"- Count: {len(liqs)}"
        )

    def _build_whale_section(self, snapshot: MarketSnapshot) -> str:
        whales = snapshot.whale_movements
        if not whales:
            return "## Whale Activity (ETH)\nNo significant whale activity"

        lines = ["## Whale Activity (ETH)"]
        for w in whales:
            action = "deposited to" if w.action == "exchange_deposit" else "withdrew from"
            lines.append(
                f"- {w.amount_eth:.1f} ETH {action} {w.exchange} "
                f"(significance: {w.significance})"
            )
        return "\n".join(lines)

    def _build_price_context_section(self, candles_context: dict) -> str:
        if not candles_context:
            return "## Price Context\nNot available"

        lines = ["## Price Context"]
        for tf, data in candles_context.items():
            lines.append(
                f"- {tf} change: {data['pct_change']:+.3f}% "
                f"(current: {data['latest_close']}, prev: {data['prev_close']})"
            )
        return "\n".join(lines)
