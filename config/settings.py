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
    EQUAL_LEVEL_TOLERANCE_PCT: float = 0.0005  # 0.05%
    # Volumen mínimo relativo para confirmar un sweep como institucional
    SWEEP_MIN_VOLUME_RATIO: float = 2.0  # 2x el promedio

    # --- Volume Analysis ---
    # Periodos para calcular volumen promedio
    VOLUME_AVG_PERIODS: int = 20

    # --- Premium/Discount ---
    # Horas entre recálculos de zonas premium/discount
    PD_RECALC_HOURS: int = 4

    # ========================
    # AI SERVICE — Claude Filter
    # ========================
    # Modelo de Claude a usar
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    # Confianza mínima para aprobar un trade
    AI_MIN_CONFIDENCE: float = 0.60

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
    # Binance liquidation WebSocket — no polling, real-time stream
    # (no interval needed, it's a persistent WebSocket connection)

    # Coinglass — future phase
    # COINGLASS_CHECK_INTERVAL: int = 60

    # ========================
    # ETHERSCAN — Whale monitoring
    # ========================
    # Minimum ETH transfer to track (ignore dust)
    WHALE_MIN_ETH: float = 10.0
    # High significance threshold
    WHALE_HIGH_ETH: float = 100.0
    # Wallets to monitor — add real whale addresses here
    WHALE_WALLETS: List[str] = field(default_factory=lambda: [
        # Placeholder addresses — replace with real whale wallets
        # Sources: Etherscan whale label, top 100 holders, known fund addresses
    ])
    # Known exchange deposit addresses
    EXCHANGE_ADDRESSES: dict = field(default_factory=lambda: {
        # Binance hot wallets
        "0x28C6c06298d514Db089934071355E5743bf21d60": "Binance",
        "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549": "Binance",
        # Coinbase
        "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3": "Coinbase",
        "0x503828976D22510aad0201ac7EC88293211D23Da": "Coinbase",
        # OKX
        "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b": "OKX",
        # Kraken
        "0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2": "Kraken",
    })

    # ========================
    # RECONNECTION
    # ========================
    # Segundos iniciales de espera para reconexión
    RECONNECT_INITIAL_DELAY: float = 1.0
    # Máximo de segundos entre reintentos
    RECONNECT_MAX_DELAY: float = 60.0
    # Factor multiplicador para backoff exponencial
    RECONNECT_BACKOFF_FACTOR: float = 2.0


# Instancia global — importar esta en todo el proyecto
settings = Settings()