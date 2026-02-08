"""
Telegram Notifier
=================
Pushes real-time alerts to your Telegram when the bot does something.

Notifications sent:
- Copy trade executed (buy)
- Position closed (sell — TP/SL/manual)
- Signal detected but skipped (for awareness)
- Daily summary
- Error alerts (when something goes wrong)

The notifier is independent of the bot commands — it pushes TO you,
while the bot waits for commands FROM you.
"""

import telegram

from config.settings import Settings
from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramNotifier:
    """
    Sends trade alerts and notifications to your Telegram chat.

    Usage:
        notifier = TelegramNotifier(settings)
        await notifier.initialize()
        await notifier.notify_buy({...})
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot: telegram.Bot | None = None
        self.chat_id: int | None = None
        self.enabled: bool = False

    async def initialize(self) -> None:
        """Set up the Telegram bot for sending messages."""
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            logger.info("notifier_disabled", note="Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
            return

        try:
            self.chat_id = int(self.settings.telegram_chat_id)
        except ValueError:
            logger.error("notifier_bad_chat_id", value=self.settings.telegram_chat_id)
            return

        self.bot = telegram.Bot(token=self.settings.telegram_bot_token)
        self.enabled = True
        logger.info("telegram_notifier_initialized")

    async def _send(self, text: str) -> None:
        """Send a message to the configured chat. Silently fails on error."""
        if not self.enabled or not self.bot:
            return

        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as e:
            logger.error("telegram_send_failed", error=str(e))

    # =========================================================================
    # Trade Notifications
    # =========================================================================

    async def notify_buy(self, trade_data: dict) -> None:
        """Alert when the bot copies a trade (buys a token)."""
        symbol = trade_data.get("token_symbol", "???")
        amount_sol = trade_data.get("amount_sol", 0)
        price = trade_data.get("price_usd", 0)
        wallet = trade_data.get("triggered_by_wallet", "")
        short_wallet = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
        status = trade_data.get("status", "unknown")
        tx = trade_data.get("tx_signature", "")

        msg = (
            f"BUY EXECUTED\n"
            f"{'='*20}\n\n"
            f"Token: {symbol}\n"
            f"Amount: {amount_sol:.6f} SOL\n"
            f"Price: ${price:.8f}\n"
            f"Copied from: {short_wallet}\n"
            f"Status: {status}\n"
        )

        if tx:
            msg += f"\nTx: {tx[:20]}..."

        await self._send(msg)

    async def notify_sell(self, trade_data: dict) -> None:
        """Alert when a position is (partially) sold."""
        symbol = trade_data.get("token_symbol", "???")
        sol_received = trade_data.get("sol_received", 0)
        reason = trade_data.get("reason", "unknown")
        status = trade_data.get("status", "unknown")
        tx = trade_data.get("tx_signature", "")

        reason_label = {
            "take_profit": "TAKE PROFIT",
            "stop_loss": "STOP LOSS",
            "manual": "MANUAL SELL",
        }.get(reason, reason.upper())

        msg = (
            f"SELL — {reason_label}\n"
            f"{'='*20}\n\n"
            f"Token: {symbol}\n"
            f"Received: {sol_received:.6f} SOL\n"
            f"Status: {status}\n"
        )

        if tx:
            msg += f"\nTx: {tx[:20]}..."

        await self._send(msg)

    async def notify_signal_skipped(self, signal: dict, reason: str) -> None:
        """Alert when a signal is detected but not traded (for awareness)."""
        symbol = signal.get("token_symbol", signal.get("token_mint", "???")[:8])
        wallet = signal.get("wallet_address", "")
        short_wallet = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
        confidence = signal.get("confidence", 0)

        msg = (
            f"SIGNAL SKIPPED\n"
            f"{'='*20}\n\n"
            f"Token: {symbol}\n"
            f"Wallet: {short_wallet}\n"
            f"Confidence: {confidence:.2f}\n"
            f"Skip reason: {reason}"
        )

        await self._send(msg)

    async def notify_daily_summary(self, stats: dict) -> None:
        """Send the end-of-day summary."""
        msg = (
            f"Daily Summary — {stats.get('date', 'today')}\n"
            f"{'='*30}\n\n"
            f"Trades: {stats.get('trades_executed', 0)}\n"
            f"Opened: {stats.get('positions_opened', 0)}\n"
            f"Closed: {stats.get('positions_closed', 0)}\n"
            f"PnL: {stats.get('total_pnl_sol', 0):+.6f} SOL"
        )

        await self._send(msg)

    async def notify_error(self, error_type: str, details: str) -> None:
        """Alert when something goes wrong."""
        msg = (
            f"ERROR\n"
            f"{'='*20}\n\n"
            f"Type: {error_type}\n"
            f"Details: {details}"
        )

        await self._send(msg)

    async def notify_pause_state(self, paused: bool) -> None:
        """Alert when trading is paused/resumed."""
        if paused:
            msg = "TRADING PAUSED — Bot has hit the daily loss limit or was manually paused."
        else:
            msg = "TRADING RESUMED — Back online."

        await self._send(msg)

    async def notify_startup(self) -> None:
        """Send a message when the bot starts up."""
        mode = self.settings.trading_mode.upper()
        msg = (
            f"Bot Started\n"
            f"{'='*20}\n\n"
            f"Mode: {mode}\n"
            f"Position size: {self.settings.default_position_size_sol} SOL\n"
            f"Max positions: {self.settings.max_open_positions}\n"
            f"Daily loss limit: {self.settings.max_daily_loss_sol} SOL"
        )

        await self._send(msg)
