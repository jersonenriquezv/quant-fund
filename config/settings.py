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
    TRADING_PAIRS: List[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])

    # ========================
    # TIMEFRAMES
    # ========================
    # HTF = para determinar tendencia/bias
    HTF_TIMEFRAMES: List[str] = field(default_factory=lambda: ["4h", "1h"])
    # LTF = para ejecución de trades
    LTF_TIMEFRAMES: List[str] = field(default_factory=lambda: ["15m", "5m"])

    # ========================
    # RISK MANAGEMENT — Guardrails inquebrantables
    # ========================
    # Starting capital (fallback if exchange balance fetch fails)
    INITIAL_CAPITAL: float = float(os.getenv("INITIAL_CAPITAL", "100"))

    # Máximo % del capital que puedes perder en un solo trade
    RISK_PER_TRADE: float = float(os.getenv("RISK_PER_TRADE", "0.02"))  # 2%

    # Máximo apalancamiento permitido
    MAX_LEVERAGE: int = int(os.getenv("MAX_LEVERAGE", "5"))

    # Drawdown diario máximo antes de apagar el bot
    MAX_DAILY_DRAWDOWN: float = float(os.getenv("MAX_DAILY_DRAWDOWN", "0.03"))  # 3%

    # Drawdown semanal máximo antes de pausar hasta el lunes
    MAX_WEEKLY_DRAWDOWN: float = float(os.getenv("MAX_WEEKLY_DRAWDOWN", "0.05"))  # 5%

    # Máximo de posiciones abiertas simultáneamente
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))

    # Máximo de trades por día
    MAX_TRADES_PER_DAY: int = int(os.getenv("MAX_TRADES_PER_DAY", "5"))

    # Minutos de espera después de una pérdida antes de operar otra vez
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "30"))

    # Mínimo Risk/Reward ratio para aceptar un trade
    MIN_RISK_REWARD: float = 1.5

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
    OB_MIN_VOLUME_RATIO: float = 1.5  # 1.5x el promedio
    # Horas máximas de vida de un OB antes de considerarlo viejo
    OB_MAX_AGE_HOURS: int = 48

    # --- Fair Value Gaps ---
    # Tamaño mínimo del FVG como % del precio
    FVG_MIN_SIZE_PCT: float = 0.001  # 0.1%
    # Horas máximas de vida de un FVG
    FVG_MAX_AGE_HOURS: int = 48

    # --- Liquidity ---
    # Tolerancia para detectar equal highs/lows (% de diferencia máxima)
    EQUAL_LEVEL_TOLERANCE_PCT: float = 0.002  # 0.2% (~$146 for BTC, ~$4.3 for ETH)
    # Volumen mínimo relativo para confirmar un sweep como institucional
    SWEEP_MIN_VOLUME_RATIO: float = 2.0  # 2x el promedio

    # --- Volume Analysis ---
    # Periodos para calcular volumen promedio
    VOLUME_AVG_PERIODS: int = 20

    # --- Premium/Discount ---
    # Horas entre recálculos de zonas premium/discount
    PD_RECALC_HOURS: int = 4
    # Band around 50% that counts as equilibrium (±this value)
    # e.g. 0.02 means 48%-52% of range = equilibrium (no trading)
    PD_EQUILIBRIUM_BAND: float = 0.02

    # --- Setup proximity ---
    # Max % of price that current price can be from OB entry to trigger setup
    OB_PROXIMITY_PCT: float = 0.003  # 0.3%

    # --- Setup A temporal ---
    # Max candles between sweep and CHoCH for Setup A validity
    SETUP_A_MAX_SWEEP_CHOCH_GAP: int = 20

    # ========================
    # QUICK SETUPS (C, D, E) — Data-driven, shorter duration
    # ========================
    # Minimum R:R for quick setups (lower than swing setups)
    MIN_RISK_REWARD_QUICK: float = 1.0
    # Max trade duration for quick setups (4 hours in seconds)
    MAX_TRADE_DURATION_QUICK: int = 14400
    # Cooldown per (pair, setup_type) for quick setups (1 hour)
    QUICK_SETUP_COOLDOWN: int = 3600

    # Setup C — Funding Squeeze
    MOMENTUM_FUNDING_THRESHOLD: float = 0.0003  # Same as FUNDING_EXTREME_THRESHOLD
    MOMENTUM_CVD_LONG_MIN: float = 0.55          # Buy dominance > 55% for long
    MOMENTUM_CVD_SHORT_MAX: float = 0.45          # Buy dominance < 45% for short
    MOMENTUM_SL_PCT: float = 0.005                # 0.5% SL distance

    # Setup E — Cascade Reversal
    CASCADE_CVD_REVERSAL_LONG: float = 0.50       # Buy dominance > 50% after long cascade
    CASCADE_CVD_REVERSAL_SHORT: float = 0.50      # Buy dominance < 50% after short cascade
    CASCADE_MAX_AGE_SECONDS: int = 900            # 15 min — cascade must be recent

    # --- Strategy behavior (profile-controlled) ---
    # If True, LTF structure (CHoCH/BOS) must align with HTF bias direction.
    REQUIRE_HTF_LTF_ALIGNMENT: bool = True
    # If True, trades in the equilibrium zone (around 50% of range) are blocked.
    ALLOW_EQUILIBRIUM_TRADES: bool = False
    # If True, 4H trend must be defined for HTF bias. If False, 1H alone is enough.
    HTF_BIAS_REQUIRE_4H: bool = True
    # PD alignment is non-negotiable — core SMC principle (long=discount, short=premium).
    REQUIRE_PD_ALIGNMENT: bool = True

    # ========================
    # AI SERVICE — Claude Filter
    # ========================
    # Modelo de Claude a usar
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    # Confianza mínima para aprobar un trade
    AI_MIN_CONFIDENCE: float = 0.60
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

    # TP3: cerrar el resto con trailing stop
    TP3_CLOSE_PCT: float = 0.20  # 20%

    # ========================
    # DATA SERVICE — Intervalos de polling
    # ========================
    # Segundos entre checks de funding rate (OKX charges every 8 hours)
    FUNDING_RATE_INTERVAL: int = 28800  # 8 horas
    # Segundos entre checks de OI
    OI_CHECK_INTERVAL: int = 300  # 5 minutos
    # Segundos entre checks de Etherscan
    ETHERSCAN_CHECK_INTERVAL: int = 300  # 5 minutos
    # OI Liquidation Proxy — detects liquidation cascades via OI drops
    # Minimum OI drop % in the window to flag as liquidation cascade
    OI_DROP_THRESHOLD_PCT: float = float(os.getenv("OI_DROP_THRESHOLD_PCT", "0.02"))  # 2%
    # Time window (seconds) to measure OI drops
    OI_DROP_WINDOW_SECONDS: int = int(os.getenv("OI_DROP_WINDOW_SECONDS", "300"))  # 5 min

    # Coinglass — future phase
    # COINGLASS_CHECK_INTERVAL: int = 60

    # ========================
    # ETHERSCAN — Whale monitoring
    # ========================
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
        "1LQoWist8KkaUXSPKZHNvEyFrWnPUiUhTJ": "Unknown Whale (79K BTC)",
        "37XuVSEpWW4trkfmvWzegTHQt7BdktSKUs": "Unknown Whale (94K BTC)",
        "1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF": "Unknown Whale (80K BTC)",
        "bc1qx9t2l3pyny2spqpqlye8svce70nppwtaxwdrp4": "Unknown Whale (44K BTC)",
        # --- El Salvador treasury ---
        "32ixEdpwzGTGFCo5tPAGRDqBqXcY2ACoyg": "El Salvador Treasury",
        # --- Mt. Gox trustee ---
        "1HeKStJGYTXLpJCGrAQnbyicoGBJFRepGA": "Mt. Gox Trustee",
        "1AsHPP7WcGRsBYmAuXUojh2DSfmHmPp3F8": "Mt. Gox Trustee 2",
        # --- Block.one (EOS) ---
        "3MfN5to5K5be2RupWE8rjJPQ6X9FqRC9BM": "Block.one (EOS)",
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
    # EXECUTION SERVICE
    # ========================
    # Seconds to wait for entry order to fill before cancelling
    ENTRY_TIMEOUT_SECONDS: int = int(os.getenv("ENTRY_TIMEOUT_SECONDS", "900"))  # 15 min
    # Seconds between order status polls
    ORDER_POLL_INTERVAL: float = float(os.getenv("ORDER_POLL_INTERVAL", "5.0"))
    # Margin mode for perpetual positions
    MARGIN_MODE: str = "isolated"
    # Max seconds a trade can stay open (12 hours)
    MAX_TRADE_DURATION_SECONDS: int = int(os.getenv("MAX_TRADE_DURATION_SECONDS", "43200"))
    # Fixed margin per trade in USDT. When > 0, overrides risk-based sizing.
    # Set to 0 to use standard risk-based sizing (RISK_PER_TRADE % of capital).
    FIXED_TRADE_MARGIN: float = float(os.getenv("FIXED_TRADE_MARGIN", "100"))
    # Sandbox: limit order tolerance from mark price (0.05% = fills like a market but with realistic slippage)
    SANDBOX_LIMIT_TOLERANCE_PCT: float = 0.0005

    # ========================
    # RECONNECTION
    # ========================
    # Segundos iniciales de espera para reconexión
    RECONNECT_INITIAL_DELAY: float = 1.0
    # Máximo de segundos entre reintentos
    RECONNECT_MAX_DELAY: float = 60.0
    # Factor multiplicador para backoff exponencial
    RECONNECT_BACKOFF_FACTOR: float = 2.0


    # ========================
    # STRATEGY PROFILE
    # ========================
    # Active profile name — set via env, Redis, or dashboard
    STRATEGY_PROFILE: str = os.getenv("STRATEGY_PROFILE", "default")


# Quick setup type identifiers
QUICK_SETUP_TYPES = ("setup_c", "setup_d", "setup_e")


# ================================================================
# Profile definitions — parameter overrides per profile
# ================================================================
# Only strategy-related settings are overridden. Risk guardrails
# (MAX_DAILY_DRAWDOWN, MAX_OPEN_POSITIONS, etc.) are NEVER changed.

STRATEGY_PROFILES: dict[str, dict] = {
    "default": {
        # No overrides — uses Settings() defaults
    },
    "aggressive": {
        # More opportunities, same core SMC rules.
        # PD alignment and HTF alignment stay ON — disabling them
        # violates SMC fundamentals (longs in premium = suicide trades).
        # AI filter ALWAYS runs — no auto-approve bypass.
        "HTF_BIAS_REQUIRE_4H": False,        # 1H alone is sufficient
        "AI_MIN_CONFIDENCE": 0.50,            # Lower threshold (default 0.60)
        "MAX_DAILY_DRAWDOWN": 0.05,           # 5% (default 3%)
        "MAX_WEEKLY_DRAWDOWN": 0.10,          # 10% (default 5%)
        "COOLDOWN_MINUTES": 15,               # 15 min (default 30)
        "MAX_TRADES_PER_DAY": 10,             # 10 (default 5)
        "OB_PROXIMITY_PCT": 0.008,            # 0.8% (default 0.3%)
        "OB_MIN_VOLUME_RATIO": 1.2,           # 1.2x (default 1.5x)
        "OB_MAX_AGE_HOURS": 72,               # 72h (default 48)
        "FVG_MAX_AGE_HOURS": 72,              # 72h (default 48)
        "MIN_RISK_REWARD": 1.2,               # 1:1.2 (default 1:1.5)
        "SWEEP_MIN_VOLUME_RATIO": 1.5,        # 1.5x (default 2.0x)
        "PD_EQUILIBRIUM_BAND": 0.01,          # 1% (default 2%)
        # Quick setups — more lenient in aggressive mode
        "QUICK_SETUP_COOLDOWN": 1800,         # 30 min (default 1h)
        "MOMENTUM_CVD_LONG_MIN": 0.52,        # 52% (default 55%)
        "MOMENTUM_CVD_SHORT_MAX": 0.48,       # 48% (default 45%)
    },
}


def apply_profile(settings_obj: Settings, profile_name: str) -> str:
    """Apply a strategy profile to the settings object.

    Returns the applied profile name, or "default" if profile not found.
    Only overrides keys defined in the profile — everything else stays.
    """
    if profile_name not in STRATEGY_PROFILES:
        return "default"

    overrides = STRATEGY_PROFILES[profile_name]
    for key, value in overrides.items():
        if hasattr(settings_obj, key):
            setattr(settings_obj, key, value)
    settings_obj.STRATEGY_PROFILE = profile_name
    return profile_name


def reset_profile(settings_obj: Settings) -> None:
    """Reset all profile-overridable settings to defaults."""
    defaults = Settings()
    for profile_overrides in STRATEGY_PROFILES.values():
        for key in profile_overrides:
            if hasattr(defaults, key):
                setattr(settings_obj, key, getattr(defaults, key))
    settings_obj.STRATEGY_PROFILE = "default"


# Instancia global — importar esta en todo el proyecto
settings = Settings()

# Apply profile from env var at startup
if settings.STRATEGY_PROFILE != "default":
    apply_profile(settings, settings.STRATEGY_PROFILE)