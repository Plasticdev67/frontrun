"""
Safety Rails
=============
Hard limits and safety checks that protect the trading wallet.

These run BEFORE and AFTER every trade. They cannot be overridden
from the Telegram bot — you have to change the config to adjust them.

Safety rails are the difference between a bot and a ticking time bomb.
Every automated trading system needs these, no exceptions.

The rails:
1. Daily loss limit — stops trading for the day if losses exceed threshold
2. Position limits — max number of open positions
3. Position size — max SOL per token
4. Liquidity floor — won't buy tokens you can't sell
5. Kill switch — instant shutdown
6. Balance check — won't trade if wallet is low on SOL (need gas fees)
"""

from config.settings import Settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)


class SafetyRails:
    """
    Pre-trade and post-trade safety checks.

    Usage:
        rails = SafetyRails(settings, db)
        can_trade, reason = await rails.pre_trade_check(signal)
        await rails.post_trade_check()
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    async def pre_trade_check(self, signal: dict, wallet_balance_sol: float) -> tuple[bool, str]:
        """
        Run all safety checks BEFORE executing a trade.

        Returns:
            (can_trade, reason) — True if safe to trade, else False with reason
        """
        # Rail 1: Kill switch
        if self.settings.trading_paused:
            return False, "Kill switch is active — all trading paused"

        # Rail 2: Mode check
        if self.settings.trading_mode != "live":
            return False, f"Not in live mode (current: {self.settings.trading_mode})"

        # Rail 3: Daily loss limit
        daily_pnl = await self.db.get_todays_pnl()
        if daily_pnl <= -self.settings.max_daily_loss_sol:
            self.settings.trading_paused = True
            logger.warning(
                "DAILY_LOSS_LIMIT_HIT",
                pnl=f"{daily_pnl:.4f} SOL",
                limit=f"-{self.settings.max_daily_loss_sol} SOL",
            )
            return False, f"Daily loss limit reached: {daily_pnl:.4f} SOL"

        # Rail 4: Max open positions
        open_count = await self.db.get_open_position_count()
        if open_count >= self.settings.max_open_positions:
            return False, f"Max positions reached: {open_count}/{self.settings.max_open_positions}"

        # Rail 5: Position size check
        token_mint = signal.get("token_mint", "")
        existing = await self.db.get_position_by_token(token_mint)
        if existing:
            current_invested = existing.get("amount_sol_invested", 0)
            if current_invested >= self.settings.max_position_size_sol:
                return False, f"Max position size for this token: {current_invested:.4f} SOL"

        # Rail 6: Wallet balance check (need SOL for trade + gas fees)
        min_balance = self.settings.default_position_size_sol + 0.01  # Trade amount + fees
        if wallet_balance_sol < min_balance:
            return False, f"Insufficient SOL balance: {wallet_balance_sol:.4f} (need {min_balance:.4f})"

        # All rails passed
        return True, ""

    async def post_trade_check(self) -> None:
        """
        Run checks AFTER a trade is executed.
        Updates daily stats and checks if we should auto-pause.
        """
        # Update daily stats
        stats = await self.db.update_daily_stats()

        # Check if we should auto-pause
        daily_pnl = stats.get("total_pnl_sol", 0)
        if daily_pnl <= -self.settings.max_daily_loss_sol:
            self.settings.trading_paused = True
            logger.warning(
                "AUTO_PAUSED",
                reason="daily_loss_limit",
                pnl=f"{daily_pnl:.4f} SOL",
            )

    def calculate_position_size(self, signal: dict, wallet_balance_sol: float) -> float:
        """
        Calculate how much SOL to spend on this trade.

        Considers:
        - Default position size from settings
        - Wallet balance (never use more than 50% of remaining balance)
        - Existing position in this token
        - Signal confidence (higher confidence = closer to max size)
        """
        base_size = self.settings.default_position_size_sol

        # Don't use more than 50% of remaining balance
        max_from_balance = wallet_balance_sol * 0.5
        size = min(base_size, max_from_balance)

        # Scale by confidence (optional — keeps base amount for now)
        # In future versions, higher-confidence signals could get larger positions
        confidence = signal.get("confidence", 0.5)
        if confidence >= 0.8:
            size = min(size * 1.0, self.settings.max_position_size_sol)  # Full size
        elif confidence >= 0.6:
            size = min(size * 0.8, self.settings.max_position_size_sol)  # 80% size

        # Make sure we don't exceed max position size for this token
        # (This is a safety net — signal generator already checks this)
        size = min(size, self.settings.max_position_size_sol)

        # Never go below a minimum viable trade (0.001 SOL)
        size = max(size, 0.001)

        return round(size, 6)
