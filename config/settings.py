"""
Configuración centralizada del bot de trading.

TODOS los parámetros configurables del sistema están aquí.
Los agentes de Claude Code NUNCA deben hardcodear valores en la lógica —
siempre importar de aquí.

Uso:
    from config.settings import settings
    print(settings.RISK_PER_TRADE)  # 0.02
"""

import os
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import List

# Carga variables de entorno desde .env
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))


@dataclass
class Settings:
    """Configuración global del bot. Un solo lugar para todo."""

    # ========================
    # EXCHANGE — OKX
    # ========================
    OKX_API_KEY: str = os.getenv("OKX_API_KEY", "")
    OKX_SECRET: str = os.getenv("OKX_SECRET", "")
    OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")
    OKX_SANDBOX: bool = os.getenv("OKX_SANDBOX", "true").lower() == "true"

    # ========================
    # APIs EXTERNAS
    # ========================
    ETHERSCAN_API_KEY: str = os.getenv("ETHERSCAN_API_KEY", "")
    # Coinglass — future phase, not used in MVP
    # COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # ========================
    # TELEGRAM NOTIFICATIONS
    # ========================
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ========================
    # BASE DE DATOS
    # ========================
    POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT: int = int(os.getenv("POSTGRES_PORT", "5432"))
    POSTGRES_DB: str = os.getenv("POSTGRES_DB", "quant_fund")
    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "jer")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "")
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))

    # ========================
    # PARES DE TRADING
    # ========================
    # Pares activos. El bot solo opera estos.
    TRADING_PAIRS: List[str] = field(default_factory=lambda: ["ETH/USDT", "BTC/USDT"])

    # ========================
    # TIMEFRAMES
    # ========================
    # HTF = para determinar tendencia/bias
    HTF_TIMEFRAMES: List[str] = field(default_factory=lambda: ["4h", "1h"])
    # LTF = para ejecución de trades
    LTF_TIMEFRAMES: List[str] = field(default_factory=lambda: ["15m", "5m"])
    # Timeframes used for swing setup evaluation (A/B/F/G).
    # 5m OBs produce micro-SLs (<0.2%) that get eaten by commissions.
    # Detectors still run on all LTF_TIMEFRAMES (quick setups need 5m data).
    SWING_SETUP_TIMEFRAMES: List[str] = field(default_factory=lambda: ["15m"])

    # ========================
    # RISK MANAGEMENT — Guardrails inquebrantables
    # ========================
    # Starting capital (fallback if exchange balance fetch fails)
    INITIAL_CAPITAL: float = float(os.getenv("INITIAL_CAPITAL", "100"))

    # Máximo % del capital que puedes perder en un solo trade
    RISK_PER_TRADE: float = float(os.getenv("RISK_PER_TRADE", "0.02"))  # 2%

    # Máximo apalancamiento permitido
    MAX_LEVERAGE: int = int(os.getenv("MAX_LEVERAGE", "7"))

    # Drawdown diario máximo antes de apagar el bot
    MAX_DAILY_DRAWDOWN: float = float(os.getenv("MAX_DAILY_DRAWDOWN", "0.05"))  # 5%

    # Drawdown semanal máximo antes de pausar hasta el lunes
    MAX_WEEKLY_DRAWDOWN: float = float(os.getenv("MAX_WEEKLY_DRAWDOWN", "0.10"))  # 10%

    # Máximo de posiciones abiertas simultáneamente
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))

    # Máximo de trades por día
    MAX_TRADES_PER_DAY: int = int(os.getenv("MAX_TRADES_PER_DAY", "10"))

    # Minutos de espera después de una pérdida antes de operar otra vez
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "15"))

    # Mínimo Risk/Reward ratio para aceptar un trade
    MIN_RISK_REWARD: float = 1.2

    # Minimum SL distance as fraction of entry price.
    # Rejects noise trades where commissions eat the profit.
    # 0.002 = 0.2% → ETH@$2000: SL >= $4. BTC@$68K: SL >= $136.
    # Backtest: 0.2% was optimal (54.5% WR, -$588). Lower lets micro-SL
    # noise trades through. Higher filters good trades too aggressively.
    MIN_RISK_DISTANCE_PCT: float = 0.002

    # Trading fee rate per side (OKX taker: 0.05%)
    # Deducted from PnL: total_fees = (entry_notional + exit_notional) * rate
    TRADING_FEE_RATE: float = float(os.getenv("TRADING_FEE_RATE", "0.0005"))

    # Backtest fill model
    # "optimistic" = touch entry price = fill (current behavior)
    # "conservative" = price must penetrate beyond entry by FILL_BUFFER_PCT
    BACKTEST_FILL_MODE: str = os.getenv("BACKTEST_FILL_MODE", "optimistic")
    BACKTEST_FILL_BUFFER_PCT: float = float(os.getenv("BACKTEST_FILL_BUFFER_PCT", "0.001"))

    # Maximum entry slippage before closing position immediately.
    # 0.003 = 0.3% → ETH@$2000: max $6 slippage. BTC@$70K: max $210.
    # Skipped in sandbox mode (synthetic fills).
    MAX_SLIPPAGE_PCT: float = 0.003

    # Tiempo máximo (horas) que un trade puede estar abierto sin moverse
    MAX_TRADE_DURATION_HOURS: int = 12

    # ========================
    # STRATEGY — Umbrales de detección SMC
    # ========================

    # --- Market Structure ---
    # Velas de lookback para detectar swing highs/lows
    SWING_LOOKBACK: int = 5
    # % mínimo que el precio debe cerrar más allá del nivel para confirmar BOS/CHoCH
    BOS_CONFIRMATION_PCT: float = 0.001  # 0.1%

    # --- Order Blocks ---
    # Volumen mínimo relativo (vs promedio) para validar un OB
    OB_MIN_VOLUME_RATIO: float = 1.2  # 1.2x el promedio
    # Horas máximas de vida de un OB antes de considerarlo viejo
    OB_MAX_AGE_HOURS: int = 72
    # Minimum OB body size as % of price — reject micro-OBs that produce tiny SLs
    OB_MIN_BODY_PCT: float = 0.001  # 0.1%
    # OB scoring weights (must sum to 1.0)
    OB_SCORE_VOLUME_W: float = 0.35
    OB_SCORE_FRESHNESS_W: float = 0.30
    OB_SCORE_PROXIMITY_W: float = 0.20
    OB_SCORE_SIZE_W: float = 0.15

    # --- Fair Value Gaps ---
    # Tamaño mínimo del FVG como % del precio
    FVG_MIN_SIZE_PCT: float = 0.001  # 0.1%
    # Horas máximas de vida de un FVG
    FVG_MAX_AGE_HOURS: int = 72

    # --- Liquidity ---
    # Tolerancia para detectar equal highs/lows (% de diferencia máxima)
    EQUAL_LEVEL_TOLERANCE_PCT: float = 0.002  # 0.2% (~$146 for BTC, ~$4.3 for ETH)
    # Volumen mínimo relativo para confirmar un sweep como institucional
    SWEEP_MIN_VOLUME_RATIO: float = 1.5  # 1.5x el promedio

    # --- Volume Analysis ---
    # Periodos para calcular volumen promedio
    VOLUME_AVG_PERIODS: int = 20

    # --- Premium/Discount ---
    # Horas entre recálculos de zonas premium/discount
    PD_RECALC_HOURS: int = 4
    # Band around 50% that counts as equilibrium (±this value)
    # e.g. 0.02 means 48%-52% of range = equilibrium (no trading)
    PD_EQUILIBRIUM_BAND: float = 0.01

    # --- Setup proximity ---
    # Max % of price that current price can be from OB entry to trigger setup
    OB_PROXIMITY_PCT: float = 0.008  # 0.8%

    # Max distance (% of price) from current price to consider an OB for zone-based orders.
    # OBs beyond this distance are ignored to avoid absurdly distant limit orders.
    OB_MAX_DISTANCE_PCT: float = 0.08  # 8%

    # --- Setup B: FVG-OB adjacency ---
    # Max gap between FVG and OB as fraction of price to count as "adjacent".
    # 0.005 = 0.5% → for ETH@$2000, FVG and OB can be up to $10 apart.
    FVG_OB_MAX_GAP_PCT: float = 0.005
    # FVG entry percentage — where within the FVG gap to place the entry.
    # 0.50 = midpoint (old default), 0.75 = shallower (closer to price, easier fill).
    # Bullish: entry = fvg.low + pct * (fvg.high - fvg.low)
    # Bearish: entry = fvg.high - pct * (fvg.high - fvg.low)
    FVG_ENTRY_PCT: float = float(os.getenv("FVG_ENTRY_PCT", "0.75"))

    # --- Enabled setups ---
    # Only these setup types will be traded. Others are detected but discarded.
    # Backtest 60d aggressive combined A+B+D+F: 97 trades, 51.5% WR, +$7,558.
    # D added: 66.7% WR in combined (9 trades, +$2,553). Quick setup — skips AI.
    # C, E, G pending validation.
    ENABLED_SETUPS: list = field(default_factory=lambda: [
        "setup_a", "setup_b", "setup_d_bos", "setup_d_choch",
    ])

    # Setup A entry depth — fraction of OB body for entry placement.
    # 0.50 = midpoint (deeper, better R:R but lower fill rate ~18%).
    # 0.65 = shallower (easier fill, slightly worse R:R).
    SETUP_A_ENTRY_PCT: float = float(os.getenv("SETUP_A_ENTRY_PCT", "0.50"))

    # Setup A mode: "both" (default), "continuation", or "reversal".
    # "continuation": CHoCH must align with HTF bias (safe, lower volume).
    # "reversal": CHoCH must oppose HTF bias (counter-trend, higher risk).
    # "both": no alignment check (current behavior).
    SETUP_A_MODE: str = os.getenv("SETUP_A_MODE", "both")

    # --- Setup A temporal ---
    # Max candles between sweep and CHoCH for Setup A validity.
    # 40 candles = ~200min on 5m, ~10h on 15m. Backtest showed gap=40
    # produces 4x more trades than gap=20 while keeping WR>45%.
    SETUP_A_MAX_SWEEP_CHOCH_GAP: int = 40

    # ========================
    # QUICK SETUPS (C, D, E) — Data-driven, shorter duration
    # ========================
    # Minimum R:R for quick setups (lower than swing setups)
    MIN_RISK_REWARD_QUICK: float = 1.0
    # Max trade duration for quick setups (4 hours in seconds)
    MAX_TRADE_DURATION_QUICK: int = 14400
    # Cooldown per (pair, setup_type) for quick setups (1 hour)
    QUICK_SETUP_COOLDOWN: int = 3600

    # Setup D — minimum break displacement (% of price).
    # Filters weak BOS/CHoCH where price barely crossed the level.
    # 0.0 = disabled (default). 0.002 = 0.2% minimum displacement.
    SETUP_D_MIN_DISPLACEMENT_PCT: float = float(os.getenv("SETUP_D_MIN_DISPLACEMENT_PCT", "0.0"))

    # ========================
    # SETUP F HARDENING — Pure OB Retest quality filters
    # ========================
    # Max candles since BOS to be considered fresh (20 = ~5h on 15m).
    SETUP_F_MAX_BOS_AGE_CANDLES: int = 20
    # Max candle gap between OB and BOS (OB must form near the BOS impulse).
    SETUP_F_MAX_OB_BOS_GAP_CANDLES: int = 10
    # Minimum BOS displacement beyond broken level (0.2% = reject micro-breaks).
    SETUP_F_MIN_BOS_DISPLACEMENT_PCT: float = 0.002
    # Minimum composite OB score (0-1) from _score_ob(). Rejects low-quality OBs.
    SETUP_F_MIN_OB_SCORE: float = 0.35
    # Max distance from current price to entry (3% = reject zombie setups).
    SETUP_F_MAX_ENTRY_DISTANCE_PCT: float = 0.03
    # Minimum confluences (3 = BOS + OB + one of PD/volume). Higher than generic 2.
    SETUP_F_MIN_CONFLUENCES: int = 3

    # Setup C — Funding Squeeze
    MOMENTUM_FUNDING_THRESHOLD: float = 0.0003  # Same as FUNDING_EXTREME_THRESHOLD
    MOMENTUM_CVD_LONG_MIN: float = 0.52          # Buy dominance > 52% for long
    MOMENTUM_CVD_SHORT_MAX: float = 0.48          # Buy dominance < 48% for short
    MOMENTUM_SL_PCT: float = 0.005                # 0.5% SL distance

    # Setup E — Cascade Reversal
    CASCADE_CVD_REVERSAL_LONG: float = 0.50       # Buy dominance > 50% after long cascade
    CASCADE_CVD_REVERSAL_SHORT: float = 0.50      # Buy dominance < 50% after short cascade
    CASCADE_MAX_AGE_SECONDS: int = 900            # 15 min — cascade must be recent

    # --- Expectancy filters (post-detection, pre-AI) ---
    # Minimum ATR(14) as fraction of price. Rejects low-volatility setups.
    MIN_ATR_PCT: float = float(os.getenv("MIN_ATR_PCT", "0.0025"))  # 0.25%
    # Minimum open space to nearest opposing structure as multiple of risk.
    MIN_TARGET_SPACE_R: float = float(os.getenv("MIN_TARGET_SPACE_R", "1.2"))

    # --- Strategy behavior (profile-controlled) ---
    # If True, LTF structure (CHoCH/BOS) must align with HTF bias direction.
    REQUIRE_HTF_LTF_ALIGNMENT: bool = False
    # If True, trades in the equilibrium zone (around 50% of range) are blocked.
    ALLOW_EQUILIBRIUM_TRADES: bool = True
    # If True, 4H trend must be defined for HTF bias. If False, 1H alone is enough.
    HTF_BIAS_REQUIRE_4H: bool = False
    # PD alignment is non-negotiable — core SMC principle (long=discount, short=premium).
    REQUIRE_PD_ALIGNMENT: bool = True
    # Allow PD override for setups with this many+ confluences (0 = disabled).
    # High-confluence setups can trade against PD zone to avoid total lockouts.
    PD_OVERRIDE_MIN_CONFLUENCES: int = int(os.getenv("PD_OVERRIDE_MIN_CONFLUENCES", "5"))
    # When True, PD zone becomes a confluence factor instead of a hard gate.
    # Aligned PD adds a confluence; misaligned PD omits it but does NOT reject.
    # Default False = current behavior (hard gate).
    PD_AS_CONFLUENCE: bool = os.getenv("PD_AS_CONFLUENCE", "false").lower() == "true"

    # ========================
    # AI SERVICE — Claude Filter
    # ========================
    # Modelo de Claude a usar
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    # Confianza mínima para aprobar un trade
    AI_MIN_CONFIDENCE: float = 0.50
    # Maximum seconds to wait for Claude API response
    AI_TIMEOUT_SECONDS: float = 30.0
    # Temperature for Claude responses (lower = more consistent)
    AI_TEMPERATURE: float = 0.3
    # Max tokens for Claude response
    AI_MAX_TOKENS: int = 500
    # Funding rate threshold for "extreme" interpretation (absolute value)
    FUNDING_EXTREME_THRESHOLD: float = 0.0003  # 0.03%

    # ========================
    # TAKE PROFITS
    # ========================
    # TP1: cerrar X% de posición a 1:1 RR
    TP1_CLOSE_PCT: float = 0.50  # 50%
    TP1_RR_RATIO: float = 1.0

    # TP2: cerrar X% a 1:2 RR
    TP2_CLOSE_PCT: float = 0.30  # 30%
    TP2_RR_RATIO: float = 2.0


    # ========================
    # DATA SERVICE — Intervalos de polling
    # ========================
    # Segundos entre checks de funding rate (OKX charges every 8 hours)
    FUNDING_RATE_INTERVAL: int = 28800  # 8 horas
    # Segundos entre checks de OI
    OI_CHECK_INTERVAL: int = 300  # 5 minutos
    # Segundos entre checks de Etherscan
    ETHERSCAN_CHECK_INTERVAL: int = 300  # 5 minutos
    # OI Flush Detector — detects liquidation cascades via OI drops
    # Minimum OI drop % in the window to flag as OI flush event
    OI_DROP_THRESHOLD_PCT: float = float(os.getenv("OI_DROP_THRESHOLD_PCT", "0.02"))  # 2%
    # Time window (seconds) to measure OI drops
    OI_DROP_WINDOW_SECONDS: int = int(os.getenv("OI_DROP_WINDOW_SECONDS", "300"))  # 5 min

    # Staleness thresholds — 2× the polling interval.
    # If a source's age exceeds this, it's marked stale in SnapshotHealth.
    FUNDING_STALE_MS: int = 28800 * 2 * 1000      # 16h (2× FUNDING_RATE_INTERVAL)
    OI_STALE_MS: int = 300 * 2 * 1000             # 10min (2× OI_CHECK_INTERVAL)
    CVD_STALE_MS: int = 30_000                     # 30s (recomputed every 5s)
    WHALE_STALE_MS: int = 300 * 2 * 1000           # 10min (2× ETHERSCAN_CHECK_INTERVAL)
    NEWS_STALE_MS: int = 300 * 2 * 1000            # 10min (2× NEWS_POLL_INTERVAL)

    # Coinglass — future phase
    # COINGLASS_CHECK_INTERVAL: int = 60

    # ========================
    # ETHERSCAN — Whale monitoring
    # ========================
    # Market makers — high-frequency OTC desks whose normal operations are noise.
    # Only notify on "high" significance (≥1000 ETH / ≥100 BTC). Data still
    # collected for AI context and dashboard.
    MARKET_MAKER_WALLETS: set = field(default_factory=lambda: {
        "0xad6eaa735d9df3d7696fd03984379dae02ed8862",  # Cumberland (DRW)
        "0x15abb66ba754f05cbc0165a64a11cded1543de48",  # Galaxy Digital
        "0x33566c9d8be6cf0b23795e0d380e112be9d75836",  # Galaxy Digital OTC
        "0x0000006daea1723962647b7e189d311d757fb793",  # Wintermute
        "0xb99a2c4c1c4f1fc27150681b740396f6ce1cbcf5",  # Abraxas Capital
        "0x7bbfaa2f8b2d2a613b4439be3428dfbf0f405390",  # Paxos
    })
    # Minimum ETH transfer to track
    WHALE_MIN_ETH: float = 100.0
    # High significance threshold
    WHALE_HIGH_ETH: float = 1000.0
    # Wallets to monitor — address → label
    # Individual whales, institutional funds, market makers, and foundations.
    # Excludes: exchange cold storage, smart contracts, bridges.
    WHALE_WALLETS: dict = field(default_factory=lambda: {
        # --- Individual whales ---
        "0xd8da6bf26964af9d7eed9e03e53415d37aa96045": "Vitalik Buterin",
        "0x220866b1a2219f40e72f5c628b65d54268ca3a9d": "Vitalik (Vb 3)",
        "0x2b6ed29a95753c3ad948348e3e7b1a251080ffb9": "Rain Lohmus",
        "0xa1a45e91164cdab8fa596809a9b24f8d4fdbe0f3": "Jeffrey Wilcke",
        "0x176f3dab24a159341c0509bb36b833e7fdd0a132": "Justin Sun",
        # --- Institutional funds & trading firms ---
        "0x15abb66ba754f05cbc0165a64a11cded1543de48": "Galaxy Digital",
        "0x33566c9d8be6cf0b23795e0d380e112be9d75836": "Galaxy Digital OTC",
        "0xad6eaa735d9df3d7696fd03984379dae02ed8862": "Cumberland (DRW)",
        "0xb99a2c4c1c4f1fc27150681b740396f6ce1cbcf5": "Abraxas Capital",
        "0x0000006daea1723962647b7e189d311d757fb793": "Wintermute",
        "0x376c3e5547c68bc26240d8dcc6729fff665a4448": "Iconomi MultiSig",
        "0xb61a16bda6d61d9b8ad493bf05962c5b98d1712f": "Deribit (Coinbase Custody)",
        "0x7bbfaa2f8b2d2a613b4439be3428dfbf0f405390": "Paxos",
        # --- Ethereum Foundation ---
        "0xc06145782f31030db1c40b203be6b0fd53410b6d": "Ethereum Foundation",
        # --- Unlabeled mega-wallets (>100K ETH, likely institutions) ---
        "0xca8fa8f0b631ecdb18cda619c4fc9d197c8affca": "Unknown Whale (325K ETH)",
        "0x1b3cb81e51011b549d78bf720b0d924ac763a7c2": "Unknown Whale (243K ETH)",
        "0xde6cf64ec6ad9fc35b38fff55cae1f469cbc1703": "Unknown Whale (201K ETH)",
        "0x2f2d854c1d6d5bb8936bb85bc07c28ebb42c9b10": "Unknown Whale (168K ETH)",
        "0x0161c59eef4639625b18757790da141aeb9114b5": "Unknown Whale (166K ETH)",
        "0x7d9557f7cec9a8077379e6235cfcc60b93e2ef11": "Unknown Whale (135K ETH)",
        "0x0100dc5672f702e705fc693218a3ad38fed6553d": "Unknown Whale (133K ETH)",
        "0xafa2a89cb43619677d9c72e81f6d4c8a730a1022": "Unknown Whale (122K ETH)",
        "0x8ae880b5d35305da48b63ce3e52b22d17859f293": "Unknown Whale (107K ETH)",
        "0x5b16fda29c71de07d5e0610c112e16a64baaffb0": "Unknown Whale (105K ETH)",
        "0xbed96d0840201011df1467379a5d311e0040073a": "Unknown Whale (103K ETH)",
        # --- Political / Insider ---
        "0x5be9a4959308a0d0c7bc0870e319314d8d957dbb": "World Liberty Financial (Trump)",
        "0x94845333028b1204fbe14e1278fd4adde46b22ce": "Donald Trump",
        # --- Trading Firms ---
        "0xf584f8728b874a6a5c7a8d4d387c9aae9172d621": "Jump Trading",
        "0x9507c04b10486547584c37bcbd931b2a4fee9a41": "Jump Trading 2",
        # --- VC / Funds ---
        "0x05e793ce0c6027323ac150f6d45c2344d28b6019": "a16z",
        # --- FTX/Alameda (court liquidation) ---
        "0x2faf487a4414fe77e2327f0bf4ae2a264a776ad2": "FTX Exchange",
        "0xc098b2a3aa256d2140208c3de6543aaef5cd3a94": "FTX 2",
        "0x3507e4978e0eb83315d20df86ca0b976c0e40ccb": "Alameda Research",
    })
    # Known exchange addresses — used to detect deposit/withdrawal direction
    EXCHANGE_ADDRESSES: dict = field(default_factory=lambda: {
        # Binance
        "0x28C6c06298d514Db089934071355E5743bf21d60": "Binance",
        "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549": "Binance",
        "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance",
        "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance",
        "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503": "Binance",
        # Coinbase
        "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3": "Coinbase",
        "0x503828976D22510aad0201ac7EC88293211D23Da": "Coinbase",
        # OKX
        "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b": "OKX",
        "0x539c92186f7c6cc4cbf443f26ef84c595babbca1": "OKX",
        "0xbfbbfaccd1126a11b8f84c60b09859f80f3bd10f": "OKX",
        "0x868dab0b8e21ec0a48b726a1ccf25826c78c6d7f": "OKX",
        "0x9c22a4039f269e72de6b029b273be059cdbb831c": "OKX",
        # Kraken
        "0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2": "Kraken",
        "0x9f1799fb47b1514f453bcebbc37ecfe883756e83": "Kraken",
        "0x8d05d9924fe935bd533a844271a1b2078eae6fcf": "Kraken",
        "0xf30ba13e4b04ce5dc4d254ae5fa95477800f0eb0": "Kraken",
        "0xd2dd7b597fd2435b6db61ddf48544fd931e6869f": "Kraken",
        # Bitfinex
        "0xe92d1a43df510f82c66382592a047d288f85226f": "Bitfinex",
        "0x8103683202aa8da10536036edef04cdd865c225e": "Bitfinex",
        "0x5a710a3cdf2af218740384c52a10852d8870626a": "Bitfinex",
        "0xc61b9bb3a7a0767e3179713f3a5c7a9aedce193c": "Bitfinex",
        # Gemini
        "0xafcd96e580138cfa2332c632e66308eacd45c5da": "Gemini",
        # Robinhood
        "0x40b38765696e3d5d8d9d834d8aad4bb6e418e489": "Robinhood",
        "0x73af3bcf944a6559933396c1577b257e2054d935": "Robinhood",
        # Upbit
        "0x0e58e8993100f1cbe45376c410f97f4893d9bfcd": "Upbit",
        # Bithumb
        "0x17e5545b11b468072283cee1f066a059fb0dbf24": "Bithumb",
        # Crypto.com
        "0xa023f08c70a23abc7edfc5b6b5e171d78dfc947e": "Crypto.com",
        # Gate.io
        "0xc882b111a75c0c657fc507c04fbfcd2cc984f071": "Gate.io",
    })

    # ========================
    # BTC WHALE MONITORING — mempool.space
    # ========================
    WHALE_MIN_BTC: float = 10.0     # ~$700K at current prices
    WHALE_HIGH_BTC: float = 100.0   # ~$7M
    MEMPOOL_CHECK_INTERVAL: int = 300  # 5 min (same as Etherscan)
    # BTC whale wallets — address → label
    BTC_WHALE_WALLETS: dict = field(default_factory=lambda: {
        # --- Individual whales / unlabeled mega-wallets ---
        "bc1q8yj0herd4r4yxszw3nkfvt53433thk0f5qst4g": "Unknown Whale (78K BTC)",
        "bc1qa5wkgaew2dkv56kfvj49j0av5nml45x9ek9hz6": "US Gov (Silk Road)",
        "bc1qazcm763858nkj2dj986etajv6wquslv8uxwczt": "Bitfinex Hack Recovery",
        # Removed: 1LQoWist8K (79K whale), 32ixEdpwzG (El Salvador),
        # 1HeKStJGY (Mt. Gox), 1AsHPP7Wc (Mt. Gox 2), 3MfN5to5K (Block.one)
        # — mempool.space returns HTTP 400 "Invalid Bitcoin address" for these
        "37XuVSEpWW4trkfmvWzegTHQt7BdktSKUs": "Unknown Whale (94K BTC)",
        "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF": "Unknown Whale (80K BTC)",
        "bc1qx9t2l3pyny2spqpqlye8svce70nppwtaxwdrp4": "Unknown Whale (44K BTC)",
        # --- UK Government (seized BTC, ~61K BTC) ---
        "bc1q4vxn43l44h30nkluqfxd9eckf45vr2awz38lwa": "UK Government",
        "bc1q7ydrtdn8z62xhslqyqtyt38mm4e2c4h3mxjkug": "UK Government 2",
    })
    # Known BTC exchange addresses — used to detect deposit/withdrawal direction
    BTC_EXCHANGE_ADDRESSES: dict = field(default_factory=lambda: {
        # Binance
        "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo": "Binance",
        "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6": "Binance",
        "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h": "Binance",
        # Coinbase
        "3Kzh9qAqVWQhEsfQz7zEQL1EuSx5tyNLNS": "Coinbase",
        "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh": "Coinbase",
        "bc1q7e6qu5smalrpgqrx9k2gnf0hgjyref5p36ru2m": "Coinbase",
        # Gemini
        "3Bi8Vq4E6dyTpwtp5BoE8NNgUaC5X49zqF": "Gemini",
        # Robinhood
        "bc1ql49ydapnjafl5t2cp9zqpjwe6pdgmxy98859v2": "Robinhood",
        # Bitfinex
        "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97": "Bitfinex",
        "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j": "Bitfinex",
        # OKX
        "bc1q2s3rjwvam9dt2ftt4sqxqjf3twav0gdx0lt02y": "OKX",
        # Kraken
        "bc1qxhmdufsvnuaaaer4ynz88fspdsxq2h9e9cetdj": "Kraken",
        "3AfP4FT2oXCBT2E5gTrQayNnGFGmjayY1Q": "Kraken",
        # Crypto.com
        "bc1q4c8n5t00jmj8temxdgcc3t32nkg2wjwz24lywv": "Crypto.com",
        # Gate.io
        "1C2DYGhcnNBYmKKSzByYE4FCoFkNkMuh2H": "Gate.io",
    })

    # ========================
    # NEWS SENTIMENT
    # ========================
    NEWS_SENTIMENT_ENABLED: bool = True
    NEWS_FEAR_GREED_URL: str = "https://api.alternative.me/fng/"
    NEWS_HEADLINES_URL: str = "https://min-api.cryptocompare.com/data/v2/news/"
    NEWS_POLL_INTERVAL: int = 300                       # 5 minutes
    NEWS_FEAR_GREED_CACHE_TTL: int = 1800               # 30 minutes
    NEWS_HEADLINES_CACHE_TTL: int = 300                  # 5 minutes
    NEWS_EXTREME_FEAR_THRESHOLD: int = 5                # F&G < 5 → reject longs (only systemic crashes)
    NEWS_EXTREME_GREED_THRESHOLD: int = 85              # F&G > 85 → reject shorts

    # ========================
    # SIGNAL MODE — semi-manual trading
    # ========================
    # When True, bot detects setups and sends Telegram signals but does NOT
    # execute. User opens trades manually; bot monitors via position adoption.
    SIGNAL_ONLY: bool = os.getenv("SIGNAL_ONLY", "false").lower() == "true"

    # ========================
    # EXECUTION SERVICE
    # ========================
    # Seconds to wait for entry order to fill before cancelling (swing setups)
    ENTRY_TIMEOUT_SECONDS: int = int(os.getenv("ENTRY_TIMEOUT_SECONDS", "86400"))  # 24 hours
    # Shorter timeout for quick setups (C/D/E)
    ENTRY_TIMEOUT_QUICK_SECONDS: int = int(os.getenv("ENTRY_TIMEOUT_QUICK_SECONDS", "3600"))  # 1 hour
    # Seconds between order status polls
    ORDER_POLL_INTERVAL: float = float(os.getenv("ORDER_POLL_INTERVAL", "5.0"))
    # Margin mode for perpetual positions
    MARGIN_MODE: str = "isolated"
    # Max seconds a trade can stay open (12 hours)
    MAX_TRADE_DURATION_SECONDS: int = int(os.getenv("MAX_TRADE_DURATION_SECONDS", "43200"))
    # Fixed margin per trade in USDT. Notional = margin × leverage.
    # e.g. $20 margin × 5x = $100 notional per trade.
    # Set to 0 to disable and use TRADE_CAPITAL_PCT instead.
    FIXED_TRADE_MARGIN: float = float(os.getenv("FIXED_TRADE_MARGIN", "20"))
    # Percentage of capital to use as notional per trade (fallback if FIXED_TRADE_MARGIN=0).
    TRADE_CAPITAL_PCT: float = float(os.getenv("TRADE_CAPITAL_PCT", "0.15"))
    # Sandbox: limit order tolerance from mark price (0.05% = fills like a market but with realistic slippage)
    SANDBOX_LIMIT_TOLERANCE_PCT: float = 0.0005

    # ========================
    # HTF CAMPAIGN TRADING — Position trades on 4H timeframe
    # ========================
    # Master switch — disabled by default, enable via env var after testing
    HTF_CAMPAIGN_ENABLED: bool = os.getenv("HTF_CAMPAIGN_ENABLED", "false").lower() == "true"
    # Timeframe for setup detection (OB/FVG/sweep detection)
    HTF_CAMPAIGN_SIGNAL_TF: str = "4h"
    # Timeframe for trend bias (Daily)
    HTF_CAMPAIGN_BIAS_TF: str = "1d"
    # Max concurrent campaigns (1 = one campaign at a time across all pairs)
    HTF_MAX_CAMPAIGNS: int = 1
    # Pyramid sizing — decreasing margin per add (total $60 if all fill)
    HTF_INITIAL_MARGIN: float = float(os.getenv("HTF_INITIAL_MARGIN", "30"))
    HTF_ADD1_MARGIN: float = float(os.getenv("HTF_ADD1_MARGIN", "15"))
    HTF_ADD2_MARGIN: float = float(os.getenv("HTF_ADD2_MARGIN", "10"))
    HTF_ADD3_MARGIN: float = float(os.getenv("HTF_ADD3_MARGIN", "5"))
    # Max pyramid adds (3 adds + initial = 4 entries total)
    HTF_MAX_ADDS: int = 3
    # Campaign must be at this R:R before first add is allowed
    HTF_ADD_MIN_RR: float = 1.0
    # Max campaign duration (7 days in seconds)
    HTF_MAX_CAMPAIGN_DURATION: int = int(os.getenv("HTF_MAX_CAMPAIGN_DURATION", "604800"))
    # Entry timeout for HTF limit orders (24 hours)
    HTF_ENTRY_TIMEOUT_SECONDS: int = int(os.getenv("HTF_ENTRY_TIMEOUT_SECONDS", "86400"))
    # Enabled setup types for HTF campaigns
    HTF_ENABLED_SETUPS: list = field(default_factory=lambda: ["setup_a", "setup_b", "setup_f"])
    # Tuned OB/FVG params for 4H (wider age and proximity)
    HTF_OB_MAX_AGE_HOURS: int = 168       # 7 days (vs 48h intraday)
    HTF_OB_MAX_DISTANCE_PCT: float = 0.10  # 10% (vs 5% intraday)
    HTF_OB_PROXIMITY_PCT: float = 0.015    # 1.5% (vs 0.3% intraday)
    HTF_FVG_MAX_AGE_HOURS: int = 168       # 7 days
    HTF_MIN_RISK_DISTANCE_PCT: float = 0.005  # 0.5% (vs 0.2% intraday)
    # Exchange minimum order sizes per pair (base currency).
    # Pre-check to avoid wasting Claude API tokens on impossible trades.
    # OKX BTC-USDT-SWAP: min 0.01 contracts × 0.01 BTC/contract = 0.0001 BTC.
    # OKX ETH-USDT-SWAP: min 0.01 contracts × 0.1 ETH/contract = 0.001 ETH.
    MIN_ORDER_SIZES: dict = field(default_factory=lambda: {
        "BTC/USDT": 0.0001,
        "ETH/USDT": 0.001,
    })

    # ========================
    # ALERT MANAGER
    # ========================
    ALERT_RATE_LIMIT_INFO: int = 10          # 10 per hour
    ALERT_RATE_LIMIT_WARNING: int = 5        # 5 per 15 min
    ALERT_RATE_LIMIT_CRITICAL: int = 20      # 20 per hour
    ALERT_RATE_WINDOW_INFO: int = 3600       # 1 hour
    ALERT_RATE_WINDOW_WARNING: int = 900     # 15 min
    ALERT_RATE_WINDOW_CRITICAL: int = 3600   # 1 hour
    ALERT_WHALE_BATCH_WINDOW: int = 120      # 2 min — group whales into digest
    ALERT_AUTO_SILENCE_THRESHOLD: int = 3    # alerts before auto-silence
    ALERT_AUTO_SILENCE_WINDOW: int = 300     # 5 min window
    ALERT_AUTO_SILENCE_DURATION: int = 900   # 15 min silence

    # Whale notification filtering — reduce noise
    # Only notify exchange deposits/withdrawals (skip neutral inter-wallet transfers)
    WHALE_NOTIFY_EXCHANGE_ONLY: bool = True
    # Minimum USD value to trigger a Telegram notification
    WHALE_NOTIFY_MIN_USD: float = 200_000

    # Drawdown warning threshold (fraction of MAX_DAILY_DRAWDOWN).
    # e.g. 0.66 = warn when DD reaches 66% of the daily limit.
    DD_WARNING_THRESHOLD: float = 0.66

    # ========================
    # ML INSTRUMENTATION
    # ========================
    # Feature version — increment when strategy params change in ways that
    # alter feature semantics (e.g. changing OB scoring weights, PD rules).
    ML_FEATURE_VERSION: int = 1

    # ========================
    # RECONNECTION
    # ========================
    # Segundos iniciales de espera para reconexión
    RECONNECT_INITIAL_DELAY: float = 1.0
    # Máximo de segundos entre reintentos
    RECONNECT_MAX_DELAY: float = 60.0
    # Factor multiplicador para backoff exponencial
    RECONNECT_BACKOFF_FACTOR: float = 2.0




# Quick setup type identifiers (bypass AI + use short entry timeout)
QUICK_SETUP_TYPES = ("setup_c", "setup_d", "setup_d_bos", "setup_d_choch", "setup_e")

# Setup types that bypass AI filter but keep normal (swing) entry timeout.
# setup_a: AI v2 approval rate 89.6% = no value added. Bypass until recalibrated.
# setup_b: AI v1 destroyed it (49% WR → 21.4% WR). Bypass until recalibrated.
AI_BYPASS_SETUP_TYPES = ("setup_a", "setup_b")


# Instancia global — importar esta en todo el proyecto
settings = Settings()
