"""
Prompt construction for Claude trade evaluation.

Builds structured system + user prompts from TradeSetup + MarketSnapshot.
The prompt quality determines decision quality — this is the intellectual core.
"""

from config.settings import settings
from shared.models import TradeSetup, MarketSnapshot

_SYSTEM_PROMPT_TEMPLATE = """You are a senior crypto trading analyst at a quantitative fund. Your job is to evaluate trade setups detected by an automated SMC (Smart Money Concepts) system and decide whether the current market context supports the trade.

You are a FILTER — you do NOT generate trades. The system has already detected a valid pattern with HTF alignment confirmed. Your job is to evaluate whether market conditions (funding, volume, flow) support executing it NOW.

You must respond ONLY with valid JSON in this exact format:
{{
    "confidence": <float 0.0-1.0>,
    "approved": <bool>,
    "reasoning": "<2-4 sentences. State the decisive factor first, then supporting evidence.>",
    "adjustments": {{
        "sl_price": <float or null>,
        "tp2_price": <float or null>,
        "tp3_price": <float or null>
    }},
    "warnings": ["<warning 1>", "<warning 2>"]
}}

Decision guidelines:
- confidence >= {min_confidence} AND approved=true: Trade proceeds to risk check
- confidence < {min_confidence} OR approved=false: Trade is discarded
- Approve only when the evidence is clearly supportive. No quota — reject all 10 if all 10 are bad.

Factors to evaluate:
1. FUNDING RATE: Extreme positive = overcrowded longs (caution for longs). Extreme negative = overcrowded shorts (opportunity for longs). Normal range = neutral factor.
2. CVD (Cumulative Volume Delta): CVD aligned with trade direction = confirmation. CVD diverging = warning sign. This is the strongest real-time signal — weigh it heavily.
3. LIQUIDATIONS: Recent cascade in the direction of the trade = exhaustion risk. Cascade against the trade direction = fuel for the move.
4. WHALE MOVEMENTS: Exchange deposits = potential selling pressure. Withdrawals = accumulation signal. Non-exchange transfers (transfer_out/transfer_in) = neutral/informational.
5. OPEN INTEREST: Provided as a snapshot (no trend). Use as context for market size only — do NOT try to infer OI direction from a single data point.
6. SETUP QUALITY: Evaluate the confluences listed. Each confluence is labeled as SUPPORTING (confirms the trade) or CONTEXT (informational). More supporting confluences = higher confidence.
7. RISK/REWARD: The blended R:R is provided. Below 1.5 = tighter, needs strong conviction. Above 2.0 = favorable risk profile.

CRITICAL RULES:
- HTF alignment is ALREADY guaranteed by the system — do NOT reject based on HTF direction.
- If funding rate is extreme (>0.03% or <-0.03%), increase skepticism for trades in the crowded direction.
- If major liquidation cascade just happened in the trade direction, the move may be exhausted — reduce confidence.
- If CVD diverges from trade direction across multiple timeframes (5m, 15m, 1h), the move lacks conviction — reduce confidence.
- When in doubt, reject. Capital preservation > opportunity capture."""


class PromptBuilder:
    """Builds system and evaluation prompts for Claude."""

    def build_system_prompt(self) -> str:
        """Return the system prompt with current AI_MIN_CONFIDENCE threshold."""
        return _SYSTEM_PROMPT_TEMPLATE.format(
            min_confidence=settings.AI_MIN_CONFIDENCE,
        )

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
            "setup_a": "Setup A (Liquidity Sweep + CHoCH + OB)",
            "setup_b": "Setup B (BOS + FVG + OB)",
        }

        # Compute R:R
        risk = abs(setup.entry_price - setup.sl_price)
        if risk > 0:
            rr1 = abs(setup.tp1_price - setup.entry_price) / risk
            rr2 = abs(setup.tp2_price - setup.entry_price) / risk
            rr3 = abs(setup.tp3_price - setup.entry_price) / risk
            blended_rr = (
                settings.TP1_CLOSE_PCT * rr1
                + settings.TP2_CLOSE_PCT * rr2
                + settings.TP3_CLOSE_PCT * rr3
            )
            rr_line = (
                f"- Risk: {risk:.2f} | TP1 R:R {rr1:.1f} | TP2 R:R {rr2:.1f} "
                f"| TP3 R:R {rr3:.1f} | Blended R:R {blended_rr:.2f}"
            )
        else:
            rr_line = "- Risk: 0 (invalid)"

        # Human-readable confluences
        confluence_lines = self._format_confluences(setup.confluences, setup.direction)

        return (
            f"## Trade Setup\n"
            f"- Pair: {setup.pair}\n"
            f"- Direction: {setup.direction}\n"
            f"- Type: {setup_names.get(setup.setup_type, setup.setup_type)}\n"
            f"- Entry: {setup.entry_price}\n"
            f"- Stop Loss: {setup.sl_price}\n"
            f"- TP1: {setup.tp1_price} (50% close)\n"
            f"- TP2: {setup.tp2_price} (30% close)\n"
            f"- TP3: {setup.tp3_price} (20% trailing)\n"
            f"{rr_line}\n"
            f"- HTF Bias: {setup.htf_bias} (confirmed aligned with direction)\n"
            f"- OB Timeframe: {setup.ob_timeframe}\n"
            f"- Confluences:\n{confluence_lines}"
        )

    def _format_confluences(
        self, confluences: list, direction: str
    ) -> str:
        """Format raw confluence strings into labeled, human-readable lines.

        Each confluence is tagged as SUPPORTING (confirms the trade thesis)
        or CONTEXT (informational, does not directly confirm direction).
        """
        _LABELS = {
            "liquidity_sweep_bullish": ("SUPPORTING", "Bullish liquidity sweep — stops below lows were hunted"),
            "liquidity_sweep_bearish": ("SUPPORTING", "Bearish liquidity sweep — stops above highs were hunted"),
            "choch_bullish": ("SUPPORTING", "Bullish CHoCH — LTF trend reversal confirmed up"),
            "choch_bearish": ("SUPPORTING", "Bearish CHoCH — LTF trend reversal confirmed down"),
            "bos_bullish": ("SUPPORTING", "Bullish BOS — LTF structure continuation up"),
            "bos_bearish": ("SUPPORTING", "Bearish BOS — LTF structure continuation down"),
            "pd_zone_discount": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                 "Price in discount zone (below 50% of range)"),
            "pd_zone_premium": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                "Price in premium zone (above 50% of range)"),
            "cvd_aligned_bullish": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                    "CVD 15m positive — buyers dominating"),
            "cvd_aligned_bearish": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                    "CVD 15m negative — sellers dominating"),
            "liquidation_cascade": ("SUPPORTING", "Liquidation cascade detected — institutional flow"),
            "funding_negative_long_opportunity": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                                  "Funding rate negative — shorts overcrowded"),
            "funding_extreme_positive": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                         "Funding rate extreme positive — longs overcrowded"),
            "oi_data_available": ("CONTEXT", "Open interest data present"),
        }
        lines = []
        for c in confluences:
            # Handle dynamic patterns like order_block_5m, fvg_15m, ob_volume_2.1x
            if c.startswith("order_block_"):
                tf = c.replace("order_block_", "")
                tag, desc = "SUPPORTING", f"Fresh order block on {tf}"
            elif c.startswith("fvg_"):
                tf = c.replace("fvg_", "")
                tag, desc = "SUPPORTING", f"Fair value gap on {tf}"
            elif c.startswith("ob_volume_"):
                ratio = c.replace("ob_volume_", "")
                try:
                    val = float(ratio.rstrip("x"))
                    tag = "SUPPORTING" if val >= 1.5 else "CONTEXT"
                    desc = f"OB volume {ratio} average ({'>= 1.5x strong' if tag == 'SUPPORTING' else '< 1.5x moderate'})"
                except ValueError:
                    tag, desc = "CONTEXT", c
            elif c.startswith("sweep_volume_"):
                ratio = c.replace("sweep_volume_", "")
                try:
                    val = float(ratio.rstrip("x"))
                    tag = "SUPPORTING" if val >= 2.0 else "CONTEXT"
                    desc = f"Sweep volume {ratio} average ({'>= 2x institutional' if tag == 'SUPPORTING' else '< 2x moderate'})"
                except ValueError:
                    tag, desc = "CONTEXT", c
            elif c.startswith("liquidations_usd_"):
                usd = c.replace("liquidations_usd_", "")
                try:
                    tag, desc = "SUPPORTING", f"${float(usd):,.0f} in estimated liquidations"
                except ValueError:
                    tag, desc = "CONTEXT", c
            elif c in _LABELS:
                tag, desc = _LABELS[c]
            else:
                tag, desc = "CONTEXT", c

            lines.append(f"  [{tag}] {desc}")
        return "\n".join(lines)

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
            f"## Open Interest (snapshot — no trend data)\n"
            f"- OI (USD): ${snapshot.oi.oi_usd:,.0f}\n"
            f"- OI (contracts): {snapshot.oi.oi_contracts:,.0f}"
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
            return (
                "## Recent Liquidations\n"
                "No liquidation cascades detected via OI proxy. "
                "This does NOT mean no liquidations occurred — only that OI did not "
                "drop >2% in the last 5 minutes. Weigh other factors accordingly."
            )

        total = sum(l.size_usd for l in liqs)
        long_usd = sum(l.size_usd for l in liqs if l.side == "long")
        short_usd = sum(l.size_usd for l in liqs if l.side == "short")

        return (
            f"## Recent Liquidations (OI Proxy)\n"
            f"- Estimated total: ${total:,.0f}\n"
            f"- Long liquidations: ${long_usd:,.0f}\n"
            f"- Short liquidations: ${short_usd:,.0f}\n"
            f"- Cascade events: {len(liqs)}\n"
            f"- Source: OI drop >2% in 5min (proxy, not individual events)"
        )

    def _build_whale_section(self, snapshot: MarketSnapshot) -> str:
        whales = snapshot.whale_movements
        if not whales:
            return "## Whale Activity\nNo significant whale movements in last 24h"

        _ACTION_LABELS = {
            "exchange_deposit": "deposited to",
            "exchange_withdrawal": "withdrew from",
            "transfer_out": "transferred out to",
            "transfer_in": "received from",
        }

        lines = ["## Whale Activity (last 24h)"]

        # Net flow summary — exchange deposits vs withdrawals
        deposit_usd = sum(w.amount_usd for w in whales if w.action == "exchange_deposit")
        withdrawal_usd = sum(w.amount_usd for w in whales if w.action == "exchange_withdrawal")
        if deposit_usd > 0 or withdrawal_usd > 0:
            net = withdrawal_usd - deposit_usd
            direction = "net withdrawal (bullish — accumulation)" if net > 0 else "net deposit (bearish — selling pressure)"
            lines.append(f"- Net exchange flow: ${abs(net):,.0f} {direction}")
            lines.append(f"  Deposited: ${deposit_usd:,.0f} | Withdrawn: ${withdrawal_usd:,.0f}")

        # Count by type
        n_deposits = sum(1 for w in whales if w.action == "exchange_deposit")
        n_withdrawals = sum(1 for w in whales if w.action == "exchange_withdrawal")
        n_transfers = sum(1 for w in whales if w.action in ("transfer_out", "transfer_in"))
        lines.append(f"- Movements: {n_deposits} deposits, {n_withdrawals} withdrawals, {n_transfers} transfers")

        # Individual movements (exchange movements first, then transfers)
        exchange_moves = [w for w in whales if w.action in ("exchange_deposit", "exchange_withdrawal")]
        other_moves = [w for w in whales if w.action not in ("exchange_deposit", "exchange_withdrawal")]

        if exchange_moves:
            lines.append("Exchange movements:")
            for w in exchange_moves:
                action = _ACTION_LABELS.get(w.action, w.action)
                label = w.wallet_label or (w.wallet[:8] + "...")
                usd_part = f" (${w.amount_usd:,.0f})" if w.amount_usd > 0 else ""
                lines.append(
                    f"  [{w.significance.upper()}] {label}: "
                    f"{w.amount:.2f} {w.chain}{usd_part} {action} {w.exchange}"
                )

        if other_moves:
            lines.append("Other transfers:")
            for w in other_moves:
                action = _ACTION_LABELS.get(w.action, w.action)
                label = w.wallet_label or (w.wallet[:8] + "...")
                usd_part = f" (${w.amount_usd:,.0f})" if w.amount_usd > 0 else ""
                lines.append(
                    f"  [{w.significance.upper()}] {label}: "
                    f"{w.amount:.2f} {w.chain}{usd_part} {action} {w.exchange}"
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
