"""
Prompt construction for Claude trade evaluation.

Builds structured system + user prompts from TradeSetup + MarketSnapshot.
The prompt quality determines decision quality — this is the intellectual core.
"""

from config.settings import settings
from shared.models import TradeSetup, MarketSnapshot

PROMPT_VERSION = "scoring_rubric_v2"

_SYSTEM_PROMPT_TEMPLATE = """You are a trade filter for an automated crypto trading system. You evaluate whether detected setups have sufficient evidence to proceed. You do not predict market direction — you assess whether available data supports or contradicts the trade thesis.

EVALUATION METHOD — Score each dimension 0-5:

1. setup_quality: Strength of technical confluences
   - 0-1: Weak (few confluences, low OB volume)
   - 2-3: Moderate (2-3 confluences, adequate volume)
   - 4-5: Strong (4+ confluences, OB volume >2x average)

2. market_support: Available data supporting the trade
   - 0: No supporting data present
   - 1-2: Weak or partial support
   - 3-4: Multiple supporting signals
   - 5: Strong multi-factor alignment

3. contradiction: Evidence AGAINST the trade
   - 0: No contradictions detected
   - 1-2: Minor or single contradiction
   - 3-4: Multiple contradicting signals
   - 5: Strong multi-factor contradiction

4. data_sufficiency: How much relevant data is available
   - 0-1: Most data fields absent
   - 2-3: Partial data available
   - 4-5: Comprehensive data present

DECISION RULES:
- APPROVE: setup_quality >= 3 AND contradiction <= 2 AND confidence >= {min_confidence}
- REJECT if: contradiction >= 3, OR setup_quality <= 1, OR insufficient supporting evidence
- "Insufficient edge" is a VALID rejection — not every setup deserves approval
- Approval REQUIRES positive evidence, not just absence of contradiction
- Absent data ("Not available") is neutral — do not penalize, do not reward

CONFIDENCE CALIBRATION:
- 0.80+: Strong setup + supporting data + no contradictions
- 0.60-0.79: Good setup + partial support or minor contradictions
- 0.50-0.59: Marginal — setup quality carries it despite limited or mixed data
- Below 0.50: Contradictions outweigh support, or insufficient edge

FACTOR READING GUIDE:

FUNDING RATE: Extreme values (>±0.03%) indicate directional crowding. The crowded side is vulnerable to forced exits on adverse moves. Normal range: no signal.

CVD: Context-dependent. For reversal setups (Setup A, after sweep + CHoCH): counter-directional CVD is expected post-sweep — not a contradiction. For continuation setups (Setup B/F, after BOS): aligned CVD = support, diverging CVD = mild contradiction. Never sufficient alone to reject.

LIQUIDATIONS: Recent cascade in trade direction: directional fuel may be spent (mild negative). Cascade against trade direction: opposing positions cleared (mild positive). Absent: neutral.

WHALES: Net exchange withdrawals reduce available sell-side supply (mild positive for longs). Net exchange deposits increase supply (mild positive for shorts). Single movements = noise, patterns of 3+ = weak signal. Absent: neutral.

NEWS (Fear & Greed): Extreme readings (<20 or >80) provide contrarian context. Normal range (25-75): no signal. Absent: neutral.

OPEN INTEREST: Snapshot only — no trend inference possible. Market size context only.

HTF BIAS: Aligned with trade = contextual support. Counter-trend = note as risk factor, but not automatic rejection — LTF structure breaks can lead HTF turns.

R:R: Below 1.5 = tight, needs stronger supporting evidence. Above 2.0 = favorable error margin.

OUTPUT — respond ONLY with valid JSON:
{{
    "approved": <bool>,
    "confidence": <float 0.0-1.0>,
    "scores": {{
        "setup_quality": <int 0-5>,
        "market_support": <int 0-5>,
        "contradiction": <int 0-5>,
        "data_sufficiency": <int 0-5>
    }},
    "supporting_factors": ["<concise factor>", ...],
    "contradicting_factors": ["<concise factor>", ...],
    "adjustments": {{
        "sl_price": <float or null>,
        "tp2_price": <float or null>
    }},
    "warnings": ["<warning>", ...]
}}

RULES:
- confidence >= {min_confidence} AND approved=true → proceeds to risk check
- No quota — reject all if all are bad
- Be consistent: similar setups with similar data should get similar scores
- Do not rationalize — if evidence is mixed, reflect that in scores and confidence
- Each factor is a weak signal. Only combinations of multiple factors should drive decisions"""


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
            self._build_oi_flush_section(snapshot),
            self._build_whale_section(snapshot),
            self._build_news_section(snapshot),
            self._build_price_context_section(candles_context),
        ]

        return "\n\n".join(sections) + "\n\nEvaluate this setup and respond with JSON only."

    def _build_setup_section(self, setup: TradeSetup) -> str:
        setup_names = {
            "setup_a": "Setup A (Liquidity Sweep + CHoCH + OB)",
            "setup_d": "Setup D (LTF Structure Scalp)",
            "setup_d_bos": "Setup D-BOS (LTF BOS Scalp)",
            "setup_d_choch": "Setup D-CHoCH (LTF CHoCH Scalp)",
        }

        # Compute R:R
        risk = abs(setup.entry_price - setup.sl_price)
        if risk > 0:
            rr = abs(setup.tp2_price - setup.entry_price) / risk
            rr_line = f"- Risk: {risk:.2f} | R:R to TP {rr:.1f}"
        else:
            rr_line = "- Risk: 0 (invalid)"

        # Human-readable confluences
        confluence_lines = self._format_confluences(setup.confluences, setup.direction)

        # HTF position trade note
        htf_note = ""
        if setup.ob_timeframe in ("4h", "1h"):
            htf_note = (
                "\n- **MODE: HIGHER TIMEFRAME POSITION TRADE** — "
                "Weight setup quality and macro structure more heavily than short-term noise. "
                "This is a multi-day campaign, not an intraday scalp."
            )

        return (
            f"## Trade Setup\n"
            f"- Pair: {setup.pair}\n"
            f"- Direction: {setup.direction}\n"
            f"- Type: {setup_names.get(setup.setup_type, setup.setup_type)}\n"
            f"- Entry: {setup.entry_price}\n"
            f"- Stop Loss: {setup.sl_price}\n"
            f"- TP1: {setup.tp1_price} (breakeven trigger at 1:1 R:R)\n"
            f"- TP2: {setup.tp2_price} (100% close at 2:1 R:R)\n"
            f"{rr_line}\n"
            f"- HTF Bias: {setup.htf_bias} ({'aligned' if self._is_htf_aligned(setup) else 'COUNTER-TREND'})\n"
            f"- OB Timeframe: {setup.ob_timeframe}\n"
            f"- Confluences:\n{confluence_lines}"
            f"{htf_note}"
        )

    @staticmethod
    def _is_htf_aligned(setup: TradeSetup) -> bool:
        """Check if trade direction aligns with HTF bias."""
        if setup.direction == "long" and setup.htf_bias == "bullish":
            return True
        if setup.direction == "short" and setup.htf_bias == "bearish":
            return True
        return False

    def _format_confluences(
        self, confluences: list, direction: str
    ) -> str:
        """Format raw confluence strings into labeled, human-readable lines.

        Each confluence is tagged as SUPPORTING (confirms the trade thesis)
        or CONTEXT (informational, does not directly confirm direction).
        """
        _LABELS = {
            "liquidity_sweep_bullish": ("SUPPORTING", "Bullish liquidity sweep detected (lows taken)"),
            "liquidity_sweep_bearish": ("SUPPORTING", "Bearish liquidity sweep detected (highs taken)"),
            "choch_bullish": ("SUPPORTING", "Bullish CHoCH confirmed on LTF"),
            "choch_bearish": ("SUPPORTING", "Bearish CHoCH confirmed on LTF"),
            "bos_bullish": ("SUPPORTING", "Bullish BOS confirmed on LTF"),
            "bos_bearish": ("SUPPORTING", "Bearish BOS confirmed on LTF"),
            "pd_zone_discount": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                 "Price in discount zone (below 50% of range)"),
            "pd_zone_premium": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                "Price in premium zone (above 50% of range)"),
            "cvd_aligned_bullish": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                    "CVD 15m positive (buy dominance)"),
            "cvd_aligned_bearish": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                    "CVD 15m negative (sell dominance)"),
            "oi_flush": ("SUPPORTING", "OI flush event detected (OI proxy)"),
            # Funding tiers — graduated crowding signal
            "funding_mild_long": ("CONTEXT", "Funding mildly negative (slight short crowding)"),
            "funding_moderate_long": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                      "Funding moderately negative (short-side crowding)"),
            "funding_extreme_long": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                     "Funding extremely negative (heavy short crowding)"),
            "funding_mild_short": ("CONTEXT", "Funding mildly positive (slight long crowding)"),
            "funding_moderate_short": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                       "Funding moderately positive (long-side crowding)"),
            "funding_extreme_short": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                      "Funding extremely positive (heavy long crowding)"),
            # Legacy labels (backward compat for old trades in DB)
            "funding_negative_long_opportunity": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                                  "Funding rate negative (short-side crowding)"),
            "funding_extreme_positive": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                         "Funding rate extreme positive (long-side crowding)"),
            # Sweep tiers
            "sweep_strong": ("SUPPORTING", "Strong sweep volume (2.5-4x average)"),
            "sweep_extreme": ("SUPPORTING", "Extreme sweep volume (4x+ average)"),
            # Buy/sell dominance tiers
            "buy_dominance_strong": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                     "Strong buy dominance (60%+)"),
            "buy_dominance_moderate": ("SUPPORTING" if direction == "long" else "CONTEXT",
                                       "Moderate buy dominance (55%+)"),
            "sell_dominance_strong": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                      "Strong sell dominance (60%+)"),
            "sell_dominance_moderate": ("SUPPORTING" if direction == "short" else "CONTEXT",
                                        "Moderate sell dominance (55%+)"),
            # OI delta tiers
            "oi_rising_mild": ("CONTEXT", "OI rising mildly (0.5-2%)"),
            "oi_rising_moderate": ("SUPPORTING", "OI rising moderately (2-5%, new positioning)"),
            "oi_rising_strong": ("SUPPORTING", "OI rising strongly (5%+, heavy institutional activity)"),
            # CVD momentum
            "cvd_momentum_confirmed": ("SUPPORTING", "CVD confirms momentum direction"),
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
                    desc = f"Sweep volume {ratio} average ({'>= 2x' if tag == 'SUPPORTING' else '< 2x'})"
                except ValueError:
                    tag, desc = "CONTEXT", c
            elif c.startswith("oi_flush_usd_"):
                usd = c.replace("oi_flush_usd_", "")
                try:
                    tag, desc = "SUPPORTING", f"${float(usd):,.0f} in estimated OI flush"
                except ValueError:
                    tag, desc = "CONTEXT", c
            elif c.startswith("oi_delta_"):
                # Raw OI delta — informational for ML, skip in prompt
                continue
            elif c.startswith("oi_dropping_"):
                pct = c.replace("oi_dropping_", "").replace("pct", "")
                tag, desc = "CONTEXT", f"OI dropping {pct}% (liquidation pressure)"
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
        abs_rate = abs(rate)
        pct = abs_rate * 100
        side = "long" if rate > 0 else "short"
        if abs_rate >= settings.FUNDING_EXTREME_THRESHOLD:
            return f"Extreme {('positive' if rate > 0 else 'negative')} ({pct:.3f}%): heavy directional crowding on {side} side"
        elif abs_rate >= settings.FUNDING_MODERATE_THRESHOLD:
            return f"Moderate {('positive' if rate > 0 else 'negative')} ({pct:.3f}%): directional crowding on {side} side"
        elif abs_rate >= settings.FUNDING_MILD_THRESHOLD:
            return f"Mild {('positive' if rate > 0 else 'negative')} ({pct:.4f}%): slight crowding on {side} side"
        elif rate > 0:
            return f"Slightly positive ({pct:.4f}%): normal range, no signal"
        elif rate < 0:
            return f"Slightly negative ({pct:.4f}%): normal range, no signal"
        return "Neutral (0%)"

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

    def _build_oi_flush_section(self, snapshot: MarketSnapshot) -> str:
        flushes = snapshot.recent_oi_flushes
        if not flushes:
            return (
                "## Recent OI Flush Events\n"
                "No OI flush events detected via OI proxy. "
                "This does NOT mean no liquidations occurred — only that OI did not "
                "drop >2% in the last 5 minutes. Weigh other factors accordingly."
            )

        total = sum(f.size_usd for f in flushes)
        long_usd = sum(f.size_usd for f in flushes if f.side == "long")
        short_usd = sum(f.size_usd for f in flushes if f.side == "short")

        return (
            f"## Recent OI Flush Events (OI Proxy)\n"
            f"- Estimated total: ${total:,.0f}\n"
            f"- Long side: ${long_usd:,.0f}\n"
            f"- Short side: ${short_usd:,.0f}\n"
            f"- Flush events: {len(flushes)}\n"
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
            flow_dir = "net withdrawal" if net > 0 else "net deposit"
            lines.append(f"- Net exchange flow: ${abs(net):,.0f} {flow_dir}")
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

    def _build_news_section(self, snapshot: MarketSnapshot) -> str:
        if snapshot.news_sentiment is None:
            return "## News Sentiment\nNot available"

        s = snapshot.news_sentiment
        lines = [
            "## News Sentiment",
            f"- Fear & Greed Index: {s.score}/100 ({s.label})",
        ]

        if s.headlines:
            lines.append("Recent headlines:")
            for h in s.headlines:
                tag = f", {h.sentiment}" if h.sentiment else ""
                lines.append(f'- "{h.title}" ({h.source}{tag})')

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
