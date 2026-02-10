"""
Wallet Refresher
=================
Keeps the wallet pool alive by periodically re-scanning GMGN for smart money.

Problem: A wallet that was crushing it last month might be cold this month.
Solution: Every 6 hours, re-scan, re-rank, and swap out underperformers.

Ranking formula:
    score = profit_30d * consistency * recency * copy_perf * bot_discount

Where:
- profit_30d: 30-day realized profit from GMGN
- consistency: min(1.0, trades_30d / 100) — rewards steady traders
- recency: 1.0 if sol_balance > 5 else 0.5 — active wallets valued higher
- copy_perf: multiplier from our own copy results (default 1.0)
- bot_discount: 0.7 if bot-speed else 1.0 — trust humans more
"""

import asyncio
from datetime import datetime, timezone

from config.settings import Settings
from database.db import Database
from discovery.gmgn_client import GMGNClient
from utils.logger import get_logger

logger = get_logger(__name__)


class WalletRefresher:
    """
    Background task that refreshes the wallet pool on a schedule.

    Usage:
        refresher = WalletRefresher(settings, db)
        await refresher.refresh()  # One-shot refresh
        # Or run as background loop via main.py
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.last_refresh: datetime | None = None
        self.pool_size: int = 0

    async def refresh(self) -> dict:
        """
        Run one full wallet refresh cycle.

        Steps:
        1. Re-scan GMGN for smart money wallets
        2. Tag bot-speed wallets
        3. Get our copy P&L per wallet
        4. Rank everyone
        5. Auto-monitor top N

        Returns summary dict with stats.
        """
        logger.info("wallet_refresh_starting")
        start = datetime.now(timezone.utc)

        # Step 1: Scan GMGN for fresh smart money
        gmgn = GMGNClient(
            cf_clearance=self.settings.gmgn_cf_clearance,
            cf_bm=self.settings.gmgn_cf_bm,
        )

        try:
            fresh_wallets = await gmgn.get_smart_money_wallets(
                min_profit_30d=self.settings.sm_min_profit_30d_usd,
                min_winrate=self.settings.sm_min_winrate,
                min_buys_30d=self.settings.sm_min_buys_30d,
                max_buys_30d=self.settings.sm_max_buys_30d,
                min_sol_balance=self.settings.sm_min_sol_balance,
            )
        finally:
            gmgn.close()

        if not fresh_wallets:
            logger.warning("wallet_refresh_no_wallets", note="GMGN returned no wallets — check cookies")
            return {"status": "failed", "reason": "no_wallets_from_gmgn"}

        logger.info("wallet_refresh_fetched", count=len(fresh_wallets))

        # Step 2: Tag bot-speed wallets and upsert all into DB
        for w in fresh_wallets:
            buy_30d = w.get("gmgn_buy_30d", 0)
            sell_30d = w.get("gmgn_sell_30d", 0)
            trades_per_day = (buy_30d + sell_30d) / 30
            is_bot = trades_per_day >= self.settings.bot_speed_threshold

            w["is_bot_speed"] = is_bot
            w["source"] = "gmgn"
            w["total_score"] = 60  # Base score for GMGN-imported wallets
            w["is_monitored"] = False  # Will be set in step 5

            if is_bot:
                w["flag_reason"] = f"BOT? {trades_per_day:.0f} trades/day"

            await self.db.upsert_wallet(w)

        # Step 3: Get our copy performance per wallet
        copy_perf = await self.db.get_copy_performance_by_wallet()

        # Step 4: Rank all GMGN wallets
        all_wallets = await self.db.get_all_wallets()
        gmgn_wallets = [w for w in all_wallets if w.get("source") == "gmgn"]

        ranked = []
        for w in gmgn_wallets:
            profit_30d = w.get("gmgn_profit_30d_usd", 0) or 0
            buy_30d = w.get("gmgn_buy_30d", 0) or 0
            sell_30d = w.get("gmgn_sell_30d", 0) or 0
            sol_balance = w.get("gmgn_sol_balance", 0) or 0
            is_bot = bool(w.get("is_bot_speed"))

            # Consistency: steady traders score higher than one-hit wonders
            trades_30d = buy_30d + sell_30d
            consistency = min(1.0, trades_30d / 100)

            # Recency: active wallets with SOL to trade
            recency = 1.0 if sol_balance > 5 else 0.5

            # Copy performance: our actual results copying this wallet
            wallet_addr = w["address"]
            wallet_pnl = copy_perf.get(wallet_addr, 0)
            if wallet_pnl > 0:
                copy_mult = 1.5  # Proven winner, boost them
            elif wallet_pnl < 0:
                copy_mult = 0.5  # Lost money copying them, penalise
            else:
                copy_mult = 1.0  # No data yet, neutral

            # Bot discount: trust humans more than bots
            bot_discount = 0.7 if is_bot else 1.0

            # Final score
            score = profit_30d * consistency * recency * copy_mult * bot_discount
            ranked.append((score, w))

        # Sort by score descending
        ranked.sort(key=lambda x: x[0], reverse=True)

        # Step 5: Auto-monitor top N
        top_n = self.settings.wallet_refresh_top_n
        promoted = []
        demoted = []

        # First, un-monitor everyone (we'll re-monitor the top N)
        for _, w in ranked:
            if w.get("is_monitored"):
                await self.db.set_wallet_monitored(w["address"], False)

        # Monitor the top N
        for i, (score, w) in enumerate(ranked[:top_n]):
            was_monitored = w.get("is_monitored", False)
            await self.db.set_wallet_monitored(w["address"], True)
            if not was_monitored:
                promoted.append(w["address"][:8])

        # Track demotions
        for i, (score, w) in enumerate(ranked[top_n:]):
            was_monitored = w.get("is_monitored", False)
            if was_monitored:
                demoted.append(w["address"][:8])

        self.last_refresh = datetime.now(timezone.utc)
        self.pool_size = len(gmgn_wallets)

        duration = (self.last_refresh - start).total_seconds()

        summary = {
            "status": "success",
            "wallets_scanned": len(fresh_wallets),
            "pool_size": len(gmgn_wallets),
            "monitored": min(top_n, len(ranked)),
            "promoted": promoted,
            "demoted": demoted,
            "bot_speed_count": sum(1 for _, w in ranked if w.get("is_bot_speed")),
            "duration_seconds": round(duration, 1),
        }

        logger.info(
            "wallet_refresh_complete",
            scanned=summary["wallets_scanned"],
            pool=summary["pool_size"],
            monitored=summary["monitored"],
            promoted=len(promoted),
            demoted=len(demoted),
            bots=summary["bot_speed_count"],
            duration=f"{duration:.1f}s",
        )

        return summary
