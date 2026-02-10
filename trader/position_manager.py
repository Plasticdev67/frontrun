"""
Position Manager
================
Monitors open positions and triggers take-profit / stop-loss sells.

Runs as a background loop alongside the wallet monitor:
- Checks current price of every open position
- If price hits take-profit level -> sell that portion
- If price hits stop-loss -> sell everything
- Updates unrealized PnL for reporting

Exit strategy: "De-risk, then ride" (Session 9)
- Tiered take-profits: recover initial early, let house money run
- Different tiers per signal source type (human/bot/consensus)
- Max hold times force exits on stale positions
- Future: momentum-based exits using volume + smart wallet mirroring
"""

import asyncio
from datetime import datetime, timezone
from typing import Any
import json

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)

# Tiered exit rules by signal source type
# Each has multiple TP levels: recover initial first, then let profits ride
# sl_multiplier: price multiple at which we stop-loss (0.6 = sell at -40%)
EXIT_RULES = {
    "human": {
        "tp_levels": [
            {"multiplier": 2.0, "pct": 0.50},   # 2x: sell 50% (initial back)
            {"multiplier": 4.0, "pct": 0.50},   # 4x: sell 25% of original (lock profit)
            {"multiplier": 8.0, "pct": 1.00},   # 8x: sell remaining 25% (moonshot captured)
        ],
        "sl_multiplier": 0.6,
        "max_hold_hours": 24,
    },
    "bot": {
        "tp_levels": [
            {"multiplier": 1.5, "pct": 0.50},   # 1.5x: sell 50% (initial back fast)
            {"multiplier": 2.5, "pct": 1.00},   # 2.5x: sell remaining (bots exit quick)
        ],
        "sl_multiplier": 0.8,
        "max_hold_hours": 2,
    },
    "consensus": {
        "tp_levels": [
            {"multiplier": 2.0, "pct": 0.33},   # 2x: sell 33% (initial back)
            {"multiplier": 5.0, "pct": 0.50},   # 5x: sell 33% of original (profit locked)
            {"multiplier": 10.0, "pct": 1.00},  # 10x: sell remaining 34% (full moonshot)
        ],
        "sl_multiplier": 0.7,
        "max_hold_hours": 48,
    },
}


class PositionManager:
    """
    Manages open positions — monitors prices and triggers TP/SL.

    Usage:
        pm = PositionManager(settings, db, executor)
        await pm.initialize()
        await pm.start()  # Runs forever as background task
    """

    def __init__(self, settings: Settings, db: Database, executor: Any = None):
        self.settings = settings
        self.db = db
        self.executor = executor  # TradeExecutor for selling
        self.session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        """Set up HTTP session for price checks."""
        self.session = aiohttp.ClientSession()
        logger.info("position_manager_initialized")

    async def close(self) -> None:
        """Clean up."""
        if self.session:
            await self.session.close()

    async def start(self) -> None:
        """
        Start the position monitoring loop.
        Checks all open positions every N seconds (from settings).
        """
        logger.info("position_manager_started", interval=f"{self.settings.position_check_interval}s")

        while True:
            try:
                if self.settings.trading_paused:
                    await asyncio.sleep(self.settings.position_check_interval)
                    continue

                positions = await self.db.get_open_positions()
                if positions:
                    await self._check_positions(positions)

                await asyncio.sleep(self.settings.position_check_interval)

            except asyncio.CancelledError:
                logger.info("position_manager_stopping")
                break
            except Exception as e:
                logger.error("position_manager_error", error=str(e))
                await asyncio.sleep(self.settings.position_check_interval)

    async def _check_positions(self, positions: list[dict]) -> None:
        """Check all open positions for TP/SL triggers using wallet-type exit rules."""
        for position in positions:
            try:
                token_mint = position["token_mint"]
                token_symbol = position.get("token_symbol", token_mint[:8])

                # Get current price
                current_price = await self._get_current_price(token_mint)
                if not current_price or current_price <= 0:
                    continue

                entry_price = position["entry_price_usd"]
                if not entry_price or entry_price <= 0:
                    continue

                # Calculate unrealized PnL
                price_multiplier = current_price / entry_price
                invested = position["amount_sol_invested"]
                unrealized_pnl = invested * (price_multiplier - 1)

                # Update position with current price
                await self.db.update_position_price(
                    position["id"], current_price, unrealized_pnl
                )

                # Get exit rules for this position's signal source type
                source_type = position.get("signal_source_type", "human") or "human"
                rules = EXIT_RULES.get(source_type, EXIT_RULES["human"])

                # Check max hold time
                opened_at = position.get("opened_at")
                if opened_at:
                    try:
                        if isinstance(opened_at, str):
                            open_time = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                        else:
                            open_time = opened_at
                        now = datetime.now(timezone.utc)
                        hours_held = (now - open_time).total_seconds() / 3600
                        if hours_held >= rules["max_hold_hours"]:
                            logger.warning(
                                "MAX_HOLD_EXPIRED",
                                token=token_symbol,
                                source_type=source_type,
                                hours_held=f"{hours_held:.1f}h",
                                max_hours=rules["max_hold_hours"],
                            )
                            if self.executor:
                                await self.executor.execute_sell(position, 1.0, "max_hold_time")
                            continue
                    except (ValueError, TypeError):
                        pass  # Can't parse timestamp, skip hold check

                # Check stop-loss (wallet-type-aware)
                sl_multiplier = rules["sl_multiplier"]
                if price_multiplier <= sl_multiplier:
                    logger.warning(
                        "STOP_LOSS_TRIGGERED",
                        token=token_symbol,
                        source_type=source_type,
                        entry=f"${entry_price:.8f}",
                        current=f"${current_price:.8f}",
                        drop=f"{(1-price_multiplier)*100:.1f}%",
                        sl_level=f"{(1-sl_multiplier)*100:.0f}%",
                    )
                    if self.executor:
                        await self.executor.execute_sell(position, 1.0, "stop_loss")
                    continue

                # Check take-profit (tiered, wallet-type-aware)
                # Use stored TP levels if they exist, otherwise generate from exit rules
                tp_levels = position.get("take_profit_levels", "[]")
                if isinstance(tp_levels, str):
                    tp_levels = json.loads(tp_levels)

                # If no custom TP levels stored, build from wallet-type exit rules
                if not tp_levels:
                    tp_levels = [
                        {"multiplier": lv["multiplier"], "pct": lv["pct"], "hit": False}
                        for lv in rules["tp_levels"]
                    ]

                for i, level in enumerate(tp_levels):
                    if level.get("hit"):
                        continue  # Already triggered this level

                    if price_multiplier >= level["multiplier"]:
                        sell_pct = level["pct"]
                        logger.info(
                            "TAKE_PROFIT_TRIGGERED",
                            token=token_symbol,
                            source_type=source_type,
                            level=f"{level['multiplier']}x",
                            sell_pct=f"{sell_pct*100:.0f}%",
                            current_price=f"${current_price:.8f}",
                        )

                        if self.executor:
                            await self.executor.execute_sell(position, sell_pct, "take_profit")

                        # Mark this level as hit
                        tp_levels[i]["hit"] = True
                        break  # Only trigger one level per check cycle

                await asyncio.sleep(0.3)  # Rate limit between position checks

            except Exception as e:
                logger.error(
                    "position_check_error",
                    token=position.get("token_symbol", "???"),
                    error=str(e),
                )

    async def _get_current_price(self, token_mint: str) -> float | None:
        """
        Get the current USD price of a token from Birdeye.
        Falls back to Jupiter price API if Birdeye fails.
        """
        # Try Birdeye first
        try:
            url = "https://public-api.birdeye.so/defi/price"
            headers = {
                "X-API-KEY": self.settings.birdeye_api_key,
                "x-chain": "solana",
            }
            params = {"address": token_mint}

            async with self.session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    price = data.get("data", {}).get("value")
                    if price and price > 0:
                        return float(price)
        except Exception:
            pass

        # Fallback: Jupiter price API
        try:
            url = f"https://price.jup.ag/v6/price?ids={token_mint}"
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    price_data = data.get("data", {}).get(token_mint, {})
                    price = price_data.get("price")
                    if price and price > 0:
                        return float(price)
        except Exception:
            pass

        return None
