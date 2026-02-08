"""
Telegram Bot
============
Your mobile control panel for the copy trading bot.

Commands:
    /start      — Welcome message + quick status
    /status     — Bot status (mode, positions, balance)
    /positions  — Show all open positions with live PnL
    /pnl        — Today's profit/loss breakdown
    /wallets    — List monitored smart wallets
    /pause      — Toggle trading on/off (kill switch)
    /help       — Show all commands

The bot only responds to YOUR chat ID (set in .env as TELEGRAM_CHAT_ID).
Nobody else can control your bot even if they find it.
"""

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config.settings import Settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramBot:
    """
    Telegram bot for monitoring and controlling the copy trader.

    Usage:
        bot = TelegramBot(settings, db)
        await bot.initialize()
        await bot.start()  # Runs the polling loop
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.app: Application | None = None
        self.authorized_chat_id: int | None = None

    async def initialize(self) -> None:
        """Build the Telegram bot application and register commands."""
        if not self.settings.telegram_bot_token:
            logger.warning("telegram_no_token", note="TELEGRAM_BOT_TOKEN not set — bot disabled")
            return

        if self.settings.telegram_chat_id:
            try:
                self.authorized_chat_id = int(self.settings.telegram_chat_id)
            except ValueError:
                logger.error("telegram_bad_chat_id", value=self.settings.telegram_chat_id)

        self.app = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .build()
        )

        # Register command handlers
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("positions", self._cmd_positions))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self.app.add_handler(CommandHandler("wallets", self._cmd_wallets))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("help", self._cmd_help))

        logger.info("telegram_bot_initialized")

    async def start(self) -> None:
        """Start the bot polling loop. Blocks until stopped."""
        if not self.app:
            logger.warning("telegram_not_initialized")
            return

        logger.info("telegram_bot_starting")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self.app and self.app.updater.running:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("telegram_bot_stopped")

    def _is_authorized(self, update: Update) -> bool:
        """Check if the message is from the authorized user."""
        if not self.authorized_chat_id:
            return True  # No restriction set — allow all (for testing)
        return update.effective_chat.id == self.authorized_chat_id

    # =========================================================================
    # Command Handlers
    # =========================================================================

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Welcome message when user first starts the bot."""
        if not self._is_authorized(update):
            return

        mode = self.settings.trading_mode.upper()
        paused = " [PAUSED]" if self.settings.trading_paused else ""

        positions = await self.db.get_open_positions()
        wallets = await self.db.get_monitored_wallets()

        msg = (
            "Solana Copy Trading Bot\n"
            "=======================\n\n"
            f"Mode: {mode}{paused}\n"
            f"Position size: {self.settings.default_position_size_sol} SOL\n"
            f"Open positions: {len(positions)}\n"
            f"Monitored wallets: {len(wallets)}\n\n"
            "Commands:\n"
            "/status — Bot status\n"
            "/positions — Open positions\n"
            "/pnl — Today's PnL\n"
            "/wallets — Monitored wallets\n"
            "/pause — Toggle trading\n"
            "/help — All commands"
        )
        await update.message.reply_text(msg)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current bot status."""
        if not self._is_authorized(update):
            return

        mode = self.settings.trading_mode.upper()
        paused = " [PAUSED]" if self.settings.trading_paused else ""

        positions = await self.db.get_open_positions()
        wallets = await self.db.get_monitored_wallets()
        daily_pnl = await self.db.get_todays_pnl()
        todays_trades = await self.db.get_todays_trades()

        # Calculate total unrealized PnL
        total_unrealized = sum(p.get("unrealized_pnl_sol", 0) or 0 for p in positions)
        total_invested = sum(p.get("amount_sol_invested", 0) or 0 for p in positions)

        msg = (
            f"Bot Status\n"
            f"==========\n\n"
            f"Mode: {mode}{paused}\n"
            f"Position size: {self.settings.default_position_size_sol} SOL\n"
            f"Max positions: {self.settings.max_open_positions}\n"
            f"Daily loss limit: {self.settings.max_daily_loss_sol} SOL\n\n"
            f"Today's Activity\n"
            f"Trades: {len(todays_trades)}\n"
            f"Realized PnL: {daily_pnl:+.6f} SOL\n\n"
            f"Portfolio\n"
            f"Open positions: {len(positions)} / {self.settings.max_open_positions}\n"
            f"Total invested: {total_invested:.6f} SOL\n"
            f"Unrealized PnL: {total_unrealized:+.6f} SOL\n\n"
            f"Monitoring: {len(wallets)} wallets"
        )
        await update.message.reply_text(msg)

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show all open positions with live PnL."""
        if not self._is_authorized(update):
            return

        positions = await self.db.get_open_positions()

        if not positions:
            await update.message.reply_text("No open positions.")
            return

        lines = ["Open Positions\n==============\n"]

        for p in positions:
            symbol = p.get("token_symbol", p["token_mint"][:8])
            invested = p.get("amount_sol_invested", 0) or 0
            entry_price = p.get("entry_price_usd", 0) or 0
            current_price = p.get("current_price_usd", 0) or 0
            unrealized = p.get("unrealized_pnl_sol", 0) or 0

            # Price multiplier
            if entry_price > 0 and current_price > 0:
                multiplier = current_price / entry_price
                mult_str = f"{multiplier:.2f}x"
            else:
                mult_str = "—"

            pnl_emoji = "+" if unrealized >= 0 else ""
            lines.append(
                f"{symbol}\n"
                f"  Invested: {invested:.4f} SOL\n"
                f"  Entry: ${entry_price:.8f}\n"
                f"  Current: ${current_price:.8f} ({mult_str})\n"
                f"  PnL: {pnl_emoji}{unrealized:.6f} SOL\n"
            )

        await update.message.reply_text("\n".join(lines))

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show today's PnL breakdown."""
        if not self._is_authorized(update):
            return

        stats = await self.db.update_daily_stats()
        positions = await self.db.get_open_positions()
        total_unrealized = sum(p.get("unrealized_pnl_sol", 0) or 0 for p in positions)

        realized = stats.get("total_pnl_sol", 0)
        combined = realized + total_unrealized

        msg = (
            f"Today's PnL\n"
            f"===========\n\n"
            f"Trades executed: {stats.get('trades_executed', 0)}\n"
            f"Positions opened: {stats.get('positions_opened', 0)}\n"
            f"Positions closed: {stats.get('positions_closed', 0)}\n\n"
            f"Realized PnL:   {realized:+.6f} SOL\n"
            f"Unrealized PnL: {total_unrealized:+.6f} SOL\n"
            f"Combined:       {combined:+.6f} SOL"
        )
        await update.message.reply_text(msg)

    async def _cmd_wallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show monitored smart wallets."""
        if not self._is_authorized(update):
            return

        wallets = await self.db.get_monitored_wallets()

        if not wallets:
            await update.message.reply_text(
                "No monitored wallets yet.\n\n"
                "Run discovery + analysis first:\n"
                "  python main.py --discover\n"
                "  python main.py --analyze"
            )
            return

        lines = [f"Monitored Wallets ({len(wallets)})\n{'='*30}\n"]

        for w in wallets[:20]:  # Cap at 20 to avoid message too long
            addr = w["address"]
            short_addr = addr[:6] + "..." + addr[-4:]
            score = w.get("total_score", 0) or 0
            pnl = w.get("total_pnl_sol", 0) or 0
            trades = w.get("total_trades", 0) or 0
            win_rate = 0
            if trades > 0:
                win_rate = (w.get("winning_trades", 0) or 0) / trades * 100

            lines.append(
                f"{short_addr}  Score: {score:.0f}\n"
                f"  PnL: {pnl:+.2f} SOL | Trades: {trades} | Win: {win_rate:.0f}%\n"
            )

        if len(wallets) > 20:
            lines.append(f"\n... and {len(wallets) - 20} more")

        await update.message.reply_text("\n".join(lines))

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle trading on/off — the kill switch."""
        if not self._is_authorized(update):
            return

        self.settings.trading_paused = not self.settings.trading_paused

        if self.settings.trading_paused:
            msg = "TRADING PAUSED\n\nAll trading is now stopped. The bot will continue monitoring but won't execute any trades.\n\nSend /pause again to resume."
            logger.warning("trading_paused_via_telegram")
        else:
            msg = f"TRADING RESUMED\n\nTrading is back on in {self.settings.trading_mode.upper()} mode.\n\nPosition size: {self.settings.default_position_size_sol} SOL"
            logger.info("trading_resumed_via_telegram")

        await update.message.reply_text(msg)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show all available commands."""
        if not self._is_authorized(update):
            return

        msg = (
            "Commands\n"
            "========\n\n"
            "/start — Welcome + quick status\n"
            "/status — Full bot status\n"
            "/positions — Open positions with PnL\n"
            "/pnl — Today's profit/loss\n"
            "/wallets — Monitored smart wallets\n"
            "/pause — Toggle trading on/off\n"
            "/help — This message"
        )
        await update.message.reply_text(msg)
