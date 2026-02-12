"""
Trade Executor
==============
The core trading engine. Receives validated signals and executes trades.

This is the most critical module in the bot. Every line of code here
directly affects real money. It's designed to be:
- SAFE: Multiple layers of checks before any trade
- TRANSPARENT: Every decision is logged with full context
- RECOVERABLE: Failed trades are handled gracefully

The flow for a trade:
1. Signal arrives from the monitor (via signal generator validation)
2. Safety rails run pre-trade checks
3. Calculate position size
4. Get Jupiter quote (best swap price)
5. Execute the swap
6. Confirm on-chain
7. Open a position record with TP/SL levels
8. Log everything

For sells (take-profit, stop-loss):
1. Position manager detects TP/SL trigger
2. Calculate how much to sell
3. Get Jupiter quote (token -> SOL)
4. Execute the swap
5. Update position record
6. Log PnL
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.solana_client import SolanaClient
from utils.logger import get_logger
from trader.jupiter_client import JupiterClient, SOL_MINT
from trader.safety_rails import SafetyRails

logger = get_logger(__name__)


class TradeExecutor:
    """
    Executes buy and sell trades based on signals from the monitor.

    Usage:
        executor = TradeExecutor(settings, db, solana)
        await executor.initialize()
        await executor.handle_signal(signal)  # Buy signal from monitor
    """

    def __init__(self, settings: Settings, db: Database, solana: SolanaClient):
        self.settings = settings
        self.db = db
        self.solana = solana
        self.jupiter: JupiterClient | None = None
        self.safety: SafetyRails = SafetyRails(settings, db)
        self.session: aiohttp.ClientSession | None = None
        self.notifier: Any | None = None  # Optional TelegramNotifier, set from main.py

    async def initialize(self) -> None:
        """Set up the Jupiter client and HTTP session."""
        self.session = aiohttp.ClientSession()
        self.jupiter = JupiterClient(
            settings=self.settings,
            keypair=self.solana.keypair,
            session=self.session,
            rpc_url=self.solana.rpc_url,
        )
        logger.info("trade_executor_initialized", mode=self.settings.trading_mode)

    async def close(self) -> None:
        """Clean up."""
        if self.session:
            await self.session.close()

    async def handle_signal(self, signal: dict) -> dict | None:
        """
        Handle a buy signal from the wallet monitor.
        This is the main entry point for trade execution.

        Returns trade data if executed, None if skipped.
        """
        token_mint = signal["token_mint"]
        token_symbol = signal.get("token_symbol", token_mint[:8])
        wallet = signal["wallet_address"]

        logger.info(
            "processing_signal",
            token=token_symbol,
            wallet=wallet[:8] + "...",
            confidence=f"{signal.get('confidence', 0):.2f}",
        )

        # ===== DRY RUN MODE =====
        # Log what we WOULD do, but don't actually trade
        if self.settings.trading_mode == "dry_run":
            return await self._dry_run_buy(signal)

        # ===== ALERT ONLY MODE =====
        # Just record the signal, don't trade
        if self.settings.trading_mode == "alert_only":
            logger.info(
                "ALERT_SIGNAL",
                token=token_symbol,
                wallet=wallet[:8] + "...",
                note="Alert-only mode — not executing trade",
            )
            return None

        # ===== LIVE MODE =====
        return await self._execute_buy(signal)

    async def _execute_buy(self, signal: dict) -> dict | None:
        """
        Execute a real buy trade on the Solana network.
        """
        token_mint = signal["token_mint"]
        token_symbol = signal.get("token_symbol", token_mint[:8])

        # Pre-trade safety check
        balance = await self.solana.get_sol_balance(self.solana.wallet_address)
        can_trade, reason = await self.safety.pre_trade_check(signal, balance)

        if not can_trade:
            logger.warning("trade_blocked", token=token_symbol, reason=reason)
            if signal.get("signal_id"):
                await self.db.mark_signal_skipped(signal["signal_id"], reason)
            return None

        # Calculate position size
        position_size_sol = self.safety.calculate_position_size(signal, balance)
        amount_lamports = int(position_size_sol * 1_000_000_000)

        logger.info(
            "executing_buy",
            token=token_symbol,
            amount_sol=f"{position_size_sol:.6f}",
            balance_sol=f"{balance:.4f}",
        )

        # Get Jupiter quote
        quote = await self.jupiter.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount=amount_lamports,
        )

        if not quote:
            logger.error("quote_failed", token=token_symbol)
            trade_id = await self.db.insert_trade({
                "token_mint": token_mint,
                "token_symbol": token_symbol,
                "side": "buy",
                "amount_sol": position_size_sol,
                "triggered_by_wallet": signal["wallet_address"],
                "signal_id": signal.get("signal_id"),
                "status": "failed",
                "error_message": "Jupiter quote failed — no route found",
            })
            return None

        # Execute the swap
        tx_signature = await self.jupiter.execute_swap(quote)

        if not tx_signature:
            logger.error("swap_failed", token=token_symbol)
            trade_id = await self.db.insert_trade({
                "token_mint": token_mint,
                "token_symbol": token_symbol,
                "side": "buy",
                "amount_sol": position_size_sol,
                "triggered_by_wallet": signal["wallet_address"],
                "signal_id": signal.get("signal_id"),
                "status": "failed",
                "error_message": "Swap execution failed",
            })
            return None

        # Confirm the transaction on-chain
        confirmed = await self.jupiter.confirm_transaction(tx_signature)

        status = "confirmed" if confirmed else "unconfirmed"
        tokens_received = int(quote.get("outAmount", 0))
        price_usd = (signal.get("token_data") or {}).get("price_usd", 0)

        # Record the trade in our audit trail
        trade_id = await self.db.insert_trade({
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "side": "buy",
            "amount_sol": position_size_sol,
            "amount_tokens": tokens_received,
            "price_usd": price_usd,
            "triggered_by_wallet": signal["wallet_address"],
            "signal_id": signal.get("signal_id"),
            "tx_signature": tx_signature,
            "status": status,
            "slippage_actual_bps": self._calculate_actual_slippage(quote),
            "priority_fee_lamports": self.settings.priority_fee_microlamports,
        })

        # Mark the signal as executed
        if signal.get("signal_id"):
            await self.db.mark_signal_executed(signal["signal_id"], trade_id)

        # Open a position record
        if confirmed:
            stop_loss_price = price_usd * self.settings.stop_loss_multiplier if price_usd else None
            tp_levels = [
                {"multiplier": m, "pct": p, "hit": False}
                for m, p in zip(self.settings.take_profit_levels, self.settings.take_profit_percentages)
            ]

            await self.db.open_position({
                "token_mint": token_mint,
                "token_symbol": token_symbol,
                "entry_price_usd": price_usd,
                "amount_sol_invested": position_size_sol,
                "amount_tokens_held": tokens_received,
                "take_profit_levels": tp_levels,
                "stop_loss_price": stop_loss_price,
                "triggered_by_wallet": signal["wallet_address"],
            })

        # Post-trade safety check
        await self.safety.post_trade_check()

        logger.info(
            "BUY_EXECUTED" if confirmed else "BUY_UNCONFIRMED",
            token=token_symbol,
            amount_sol=f"{position_size_sol:.6f}",
            tokens_received=tokens_received,
            price=f"${price_usd:.8f}" if price_usd else "unknown",
            tx=tx_signature[:16] + "...",
        )

        return {
            "trade_id": trade_id,
            "tx_signature": tx_signature,
            "status": status,
            "amount_sol": position_size_sol,
            "tokens_received": tokens_received,
            "price_usd": price_usd,
        }

    async def _dry_run_buy(self, signal: dict) -> dict | None:
        """
        Simulate a buy trade without executing it.
        Logs what WOULD happen and records it with 'dry_run' status.
        """
        token_mint = signal["token_mint"]
        token_symbol = signal.get("token_symbol", token_mint[:8])
        position_size = signal.get("position_size_sol") or self.settings.default_position_size_sol
        price_usd = (signal.get("token_data") or {}).get("price_usd", 0)

        logger.info(
            "DRY_RUN_BUY",
            token=token_symbol,
            would_spend=f"{position_size:.6f} SOL",
            price=f"${price_usd:.8f}" if price_usd else "unknown",
            wallet=signal["wallet_address"][:8] + "...",
            confidence=f"{signal.get('confidence', 0):.2f}",
        )

        # Record in database as dry run
        trade_id = await self.db.insert_trade({
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "side": "buy",
            "amount_sol": position_size,
            "price_usd": price_usd,
            "triggered_by_wallet": signal["wallet_address"],
            "signal_id": signal.get("signal_id"),
            "status": "dry_run",
        })

        if signal.get("signal_id"):
            await self.db.mark_signal_executed(signal["signal_id"], trade_id)

        # Only open a position if we have a real price — $0 positions are useless
        if not price_usd or price_usd <= 0:
            logger.warning(
                "dry_run_no_price",
                token=token_symbol,
                note="Skipping position creation — no price data for PnL tracking",
            )
            return {"trade_id": trade_id, "status": "dry_run"}

        # Open a position record so the position manager can track TP/SL
        import json
        source_type = signal.get("signal_source_type", "human") or "human"
        from trader.position_manager import EXIT_RULES
        rules = EXIT_RULES.get(source_type, EXIT_RULES["human"])
        tp_levels = [
            {"multiplier": lv["multiplier"], "pct": lv["pct"], "hit": False}
            for lv in rules["tp_levels"]
        ]

        # Estimate tokens received (for sell calculations later)
        # In dry_run we don't have real swap output, so estimate from price
        estimated_tokens = 0
        if price_usd and price_usd > 0:
            sol_price_usd = await self._get_sol_price()
            if sol_price_usd:
                estimated_tokens = int((position_size * sol_price_usd / price_usd) * 1_000_000)

        await self.db.open_position({
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "entry_price_usd": price_usd,
            "amount_sol_invested": position_size,
            "amount_tokens_held": estimated_tokens,
            "take_profit_levels": tp_levels,
            "stop_loss_price": price_usd * rules["sl_multiplier"] if price_usd else None,
            "triggered_by_wallet": signal["wallet_address"],
            "signal_source_type": source_type,
        })

        logger.info(
            "dry_run_position_opened",
            token=token_symbol,
            source_type=source_type,
            entry_price=f"${price_usd:.8f}" if price_usd else "unknown",
            tp_levels=[f"{lv['multiplier']}x" for lv in tp_levels],
        )

        return {"trade_id": trade_id, "status": "dry_run"}

    async def execute_sell(
        self,
        position: dict,
        sell_percentage: float,
        reason: str,
    ) -> dict | None:
        """
        Sell a portion (or all) of a position.

        Args:
            position: The position record from the database
            sell_percentage: How much to sell (0.5 = 50%, 1.0 = all)
            reason: Why we're selling ("take_profit", "stop_loss", "manual")
        """
        token_mint = position["token_mint"]
        token_symbol = position.get("token_symbol", token_mint[:8])
        tokens_to_sell = int(position["amount_tokens_held"] * sell_percentage)

        if tokens_to_sell <= 0:
            logger.warning("nothing_to_sell", token=token_symbol)
            return None

        logger.info(
            "executing_sell",
            token=token_symbol,
            percentage=f"{sell_percentage*100:.0f}%",
            tokens=tokens_to_sell,
            reason=reason,
        )

        if self.settings.trading_mode == "dry_run":
            # In dry_run mode, simulate the sell and CLOSE the position in DB
            # Without this, positions stay open forever and block new trades
            entry_price = position.get("entry_price_usd", 0) or 0
            current_price = position.get("current_price_usd", 0) or 0

            if sell_percentage >= 1.0:
                # Full close — calculate simulated PnL
                invested = position.get("amount_sol_invested", 0) or 0
                if entry_price > 0 and current_price > 0:
                    multiplier = current_price / entry_price
                    simulated_pnl = invested * (multiplier - 1)
                else:
                    simulated_pnl = 0

                await self.db.close_position(position["id"], reason, simulated_pnl)
                logger.info(
                    "DRY_RUN_SELL",
                    token=token_symbol,
                    reason=reason,
                    percentage=f"{sell_percentage*100:.0f}%",
                    simulated_pnl=f"{simulated_pnl:+.6f} SOL",
                )
            else:
                # Partial sell — just log it (position stays open)
                logger.info(
                    "DRY_RUN_SELL",
                    token=token_symbol,
                    reason=reason,
                    percentage=f"{sell_percentage*100:.0f}%",
                )

            # Push Telegram notification for dry-run sells too
            if self.notifier:
                await self.notifier.notify_sell({
                    "token_symbol": token_symbol,
                    "sol_received": 0,
                    "reason": f"[DRY_RUN] {reason}",
                    "status": "dry_run",
                    "tx_signature": "dry_run",
                })

            return {"status": "dry_run", "reason": reason}

        # Get Jupiter quote (token -> SOL)
        quote = await self.jupiter.get_quote(
            input_mint=token_mint,
            output_mint=SOL_MINT,
            amount=tokens_to_sell,
        )

        if not quote:
            logger.error("sell_quote_failed", token=token_symbol)
            return None

        # Execute the sell
        tx_signature = await self.jupiter.execute_swap(quote)
        if not tx_signature:
            logger.error("sell_swap_failed", token=token_symbol)
            return None

        confirmed = await self.jupiter.confirm_transaction(tx_signature)

        sol_received = int(quote.get("outAmount", 0)) / 1_000_000_000
        status = "confirmed" if confirmed else "unconfirmed"

        # Record the sell trade
        trade_id = await self.db.insert_trade({
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "side": "sell",
            "amount_sol": sol_received,
            "amount_tokens": tokens_to_sell,
            "price_usd": position.get("current_price_usd", position.get("entry_price_usd", 0)),
            "sell_reason": reason,
            "tx_signature": tx_signature,
            "status": status,
        })

        # Update the position
        if sell_percentage >= 1.0:
            # Closing the full position
            realized_pnl = sol_received - position["amount_sol_invested"]
            await self.db.close_position(position["id"], reason, realized_pnl)
            logger.info(
                "POSITION_CLOSED",
                token=token_symbol,
                pnl=f"{realized_pnl:+.6f} SOL",
                reason=reason,
            )
        else:
            # Partial sell — update remaining tokens
            remaining = position["amount_tokens_held"] - tokens_to_sell
            logger.info(
                "PARTIAL_SELL",
                token=token_symbol,
                sol_received=f"{sol_received:.6f}",
                remaining_tokens=remaining,
                reason=reason,
            )

        await self.safety.post_trade_check()

        # Push Telegram notification for the sell
        if self.notifier:
            await self.notifier.notify_sell({
                "token_symbol": token_symbol,
                "sol_received": sol_received,
                "reason": reason,
                "status": status,
                "tx_signature": tx_signature,
            })

        return {
            "trade_id": trade_id,
            "tx_signature": tx_signature,
            "sol_received": sol_received,
            "status": status,
        }

    async def _get_sol_price(self) -> float | None:
        """Get current SOL price in USD from DexScreener (free, no key)."""
        try:
            url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs") or []
                    if pairs:
                        price = pairs[0].get("priceUsd")
                        if price:
                            return float(price)
        except Exception:
            pass
        return None

    def _calculate_actual_slippage(self, quote: dict) -> int:
        """
        Calculate the actual slippage from a Jupiter quote.
        Returns slippage in basis points.
        """
        try:
            price_impact = float(quote.get("priceImpactPct", "0"))
            return int(abs(price_impact) * 100)
        except (ValueError, TypeError):
            return 0
