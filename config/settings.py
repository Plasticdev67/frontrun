"""
Configuration Manager
=====================
This is the single source of truth for ALL bot settings.
It loads secrets (API keys, private key) from a .env file,
and defines default values for every tunable parameter.

How it works:
- On startup, it reads your .env file
- Each setting has a sensible default so the bot works out of the box for testing
- You can override anything by changing the .env file or setting environment variables
- The Settings object is created once and passed to every module that needs it
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv


# Load environment variables from .env file in the project root
# This reads your .env file and makes all values available via os.getenv()
load_dotenv(Path(__file__).parent.parent / ".env")


def _get_env(key: str, default: str = "") -> str:
    """Get an environment variable, returning default if not set."""
    return os.getenv(key, default)


def _get_env_float(key: str, default: float) -> float:
    """Get an environment variable as a float number."""
    val = os.getenv(key)
    return float(val) if val else default


def _get_env_int(key: str, default: int) -> int:
    """Get an environment variable as a whole number."""
    val = os.getenv(key)
    return int(val) if val else default


@dataclass
class Settings:
    """
    All bot configuration in one place.

    Sections:
    - API Keys: Your credentials for external services
    - Trading: How much to buy, when to sell, risk limits
    - Safety: Hard limits that protect you from bugs and bad trades
    - Wallet Scoring: Thresholds for what counts as a "smart wallet"
    - System: Database path, logging level, etc.
    """

    # =========================================================================
    # API Keys & Endpoints
    # =========================================================================

    # Solana wallet private key (base58). NEVER log or expose this.
    wallet_private_key: str = field(default_factory=lambda: _get_env("WALLET_PRIVATE_KEY"))

    # Helius — our main Solana RPC provider + webhooks
    helius_api_key: str = field(default_factory=lambda: _get_env("HELIUS_API_KEY"))
    helius_rpc_url: str = field(default_factory=lambda: _get_env(
        "HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key="
    ))

    # Birdeye — token data, prices, top traders
    birdeye_api_key: str = field(default_factory=lambda: _get_env("BIRDEYE_API_KEY"))
    birdeye_base_url: str = "https://public-api.birdeye.so"

    # DexScreener — token discovery (free, no key needed)
    dexscreener_base_url: str = "https://api.dexscreener.com"

    # Jupiter — DEX aggregator for trade execution
    jupiter_base_url: str = "https://quote-api.jup.ag/v6"

    # Telegram
    telegram_bot_token: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _get_env("TELEGRAM_CHAT_ID"))

    # =========================================================================
    # Trading Parameters
    # =========================================================================

    # How much SOL to spend per copy trade
    # Start at 0.01 SOL (~$2) for testing, scale up once you trust the system
    default_position_size_sol: float = field(
        default_factory=lambda: _get_env_float("DEFAULT_POSITION_SIZE_SOL", 0.01)
    )

    # Maximum SOL to allocate to a single token (across multiple buys)
    max_position_size_sol: float = field(
        default_factory=lambda: _get_env_float("MAX_POSITION_SIZE_SOL", 0.05)
    )

    # Slippage tolerance for Jupiter swaps
    # 300 = 3%. Memecoins need higher slippage due to volatility.
    slippage_bps: int = field(
        default_factory=lambda: _get_env_int("SLIPPAGE_BPS", 300)
    )

    # Priority fee for faster transaction inclusion on Solana
    # Higher = faster but costs more. 50000 microlamports is a good baseline.
    priority_fee_microlamports: int = field(
        default_factory=lambda: _get_env_int("PRIORITY_FEE_MICROLAMPORTS", 50000)
    )

    # Take-profit levels: sell portions of the position at these multiples
    # Example: [2.0, 5.0] means sell 50% at 2x, sell remaining at 5x
    take_profit_levels: list[float] = field(default_factory=lambda: [2.0, 5.0])

    # Percentage to sell at each take-profit level
    # Must match length of take_profit_levels
    take_profit_percentages: list[float] = field(default_factory=lambda: [0.5, 1.0])

    # Stop-loss: sell everything if price drops below this multiple
    # 0.5 means sell if the token drops 50% from entry
    stop_loss_multiplier: float = 0.5

    # How often to check positions for TP/SL (in seconds)
    position_check_interval: int = 10

    # =========================================================================
    # Safety Rails — Hard Limits
    # =========================================================================

    # Maximum open positions at any time
    # Prevents overexposure. 10 is conservative for testing.
    max_open_positions: int = field(
        default_factory=lambda: _get_env_int("MAX_OPEN_POSITIONS", 10)
    )

    # Maximum SOL loss in a single day before the bot auto-pauses
    # This is your circuit breaker. If the bot loses this much, it stops.
    max_daily_loss_sol: float = field(
        default_factory=lambda: _get_env_float("MAX_DAILY_LOSS_SOL", 0.5)
    )

    # Minimum liquidity (in USD) a token must have before we'll buy it
    # Tokens below this are too illiquid — we might not be able to sell
    min_liquidity_usd: float = field(
        default_factory=lambda: _get_env_float("MIN_LIQUIDITY_USD", 10000)
    )

    # Market cap range (in USD) for tokens we'll trade
    min_market_cap_usd: float = field(
        default_factory=lambda: _get_env_float("MIN_MARKET_CAP_USD", 1_000_000)
    )
    max_market_cap_usd: float = field(
        default_factory=lambda: _get_env_float("MAX_MARKET_CAP_USD", 50_000_000)
    )

    # Tokens we will NEVER buy, no matter what
    # Add token mint addresses here to blacklist them
    token_blacklist: list[str] = field(default_factory=list)

    # =========================================================================
    # Bot Operating Mode
    # =========================================================================

    # "live" = execute real trades
    # "dry_run" = log what WOULD happen but don't actually trade
    # "alert_only" = send Telegram alerts but don't trade
    trading_mode: str = "dry_run"

    # Master kill switch — set to True to immediately stop all trading
    trading_paused: bool = False

    # =========================================================================
    # Wallet Scoring Thresholds (used in Stage 2)
    # =========================================================================

    # Minimum score (0-100) for a wallet to be considered "smart money"
    min_wallet_score: float = 60.0

    # Maximum wallets to monitor in real-time (more = more API calls)
    max_monitored_wallets: int = 50

    # =========================================================================
    # Token Discovery Settings (used in Stage 1)
    # =========================================================================

    # How many days back to look for top-performing tokens
    discovery_lookback_days: int = 30

    # Minimum price increase (as multiplier) to count as a "winner"
    # 5.0 means the token must have done at least 5x in the lookback period
    min_price_multiplier: float = 5.0

    # Maximum tokens to analyze per discovery run
    max_discovery_tokens: int = 100

    # =========================================================================
    # System
    # =========================================================================

    # Path to the SQLite database file
    db_path: str = field(
        default_factory=lambda: str(Path(__file__).parent.parent / "data" / "copy_trader.db")
    )

    # Logging level: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"

    # How long to cache API responses (in seconds) to avoid rate limits
    api_cache_ttl: int = 60

    @property
    def helius_rpc_full_url(self) -> str:
        """Build the full Helius RPC URL with the API key."""
        if self.helius_api_key and "api-key=" not in self.helius_rpc_url:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return self.helius_rpc_url

    def validate(self) -> list[str]:
        """
        Check that all required settings are present.
        Returns a list of problems found (empty list = all good).
        """
        problems = []

        if not self.helius_api_key:
            problems.append("HELIUS_API_KEY is not set — needed for Solana RPC")
        if not self.birdeye_api_key:
            problems.append("BIRDEYE_API_KEY is not set — needed for token data")
        if not self.wallet_private_key and self.trading_mode == "live":
            problems.append("WALLET_PRIVATE_KEY is not set — needed for live trading")
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set — needed for alerts")
        if not self.telegram_chat_id:
            problems.append("TELEGRAM_CHAT_ID is not set — needed for alerts")

        # Validate trading parameters make sense
        if self.default_position_size_sol > self.max_position_size_sol:
            problems.append("DEFAULT_POSITION_SIZE_SOL is larger than MAX_POSITION_SIZE_SOL")
        if self.stop_loss_multiplier >= 1.0:
            problems.append("STOP_LOSS_MULTIPLIER should be less than 1.0 (e.g., 0.5 = sell at 50% loss)")
        if len(self.take_profit_levels) != len(self.take_profit_percentages):
            problems.append("TAKE_PROFIT_LEVELS and TAKE_PROFIT_PERCENTAGES must have the same length")

        return problems


# Create a global settings instance that other modules can import
# Usage: from config.settings import settings
settings = Settings()
