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

Bot detection (smarter than raw trade count):
- Primary: GMGN tags (sandwich_bot, sniper_bot, mev_bot, copy_bot, arb_bot)
- Secondary: 200+ trades/day (clearly automated — real degens do 50-150/day)
"""

import asyncio
import json
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
        # Bot detection: GMGN tags are primary signal (most reliable),
        # raw trade frequency is secondary (200+/day = clearly automated).
        # Real degens easily do 50-150 trades/day — that's human behavior.
        BOT_TAGS = {"sandwich_bot", "sniper_bot", "mev_bot", "copy_bot", "arb_bot"}

        for w in fresh_wallets:
            buy_30d = w.get("gmgn_buy_30d", 0) or 0
            sell_30d = w.get("gmgn_sell_30d", 0) or 0
            trades_per_day = (buy_30d + sell_30d) / 30

            # Parse GMGN tags
            raw_tags = w.get("gmgn_tags") or []
            if isinstance(raw_tags, str):
                try:
                    raw_tags = json.loads(raw_tags)
                except (json.JSONDecodeError, TypeError):
                    raw_tags = []
            tag_set = set(raw_tags)

            # Primary: GMGN tagged as bot type
            is_bot_by_tag = bool(tag_set & BOT_TAGS)
            # Secondary: trade frequency clearly automated (200+/day)
            is_bot_by_speed = trades_per_day >= self.settings.bot_speed_threshold

            is_bot = is_bot_by_tag or is_bot_by_speed

            w["is_bot_speed"] = is_bot
            w["source"] = "gmgn"
            w["total_score"] = 60  # Base score — will be overridden by ranking
            w["is_monitored"] = False  # Will be set in step 5

            if is_bot_by_tag:
                w["flag_reason"] = f"BOT: GMGN tagged {tag_set & BOT_TAGS}"
            elif is_bot_by_speed:
                w["flag_reason"] = f"BOT? {trades_per_day:.0f} trades/day"

            await self.db.upsert_wallet(w)

        # Step 3: Get our copy performance per wallet
        copy_perf = await self.db.get_copy_performance_by_wallet()

        # Step 4: Score and rank all GMGN wallets (0-100 composite score)
        all_wallets = await self.db.get_all_wallets()
        gmgn_wallets = [w for w in all_wallets if w.get("source") == "gmgn"]

        ranked = []
        for w in gmgn_wallets:
            score = self._compute_wallet_score(w, copy_perf)

            # Store the computed score back to DB
            await self.db.update_wallet_score(w["address"], score)

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

    def _compute_wallet_score(self, w: dict, copy_perf: dict | None = None) -> float:
        """
        Calculate composite 0-100 score for a wallet.

        Components (max 100 points):
        - Profit (40 pts): 30-day realized profit, capped at $100K
        - Win Rate (25 pts): GMGN winrate percentage
        - Consistency (20 pts): Steady trading activity over 30 days
        - Balance (10 pts): SOL available to trade with
        - Bot Penalty (-10 pts): If flagged as bot, lose 10 points
        - Copy Bonus (+5/-5 pts): Our actual results copying this wallet
        """
        if copy_perf is None:
            copy_perf = {}

        profit_30d = w.get("gmgn_profit_30d_usd", 0) or 0
        winrate = w.get("gmgn_winrate", 0) or 0
        buy_30d = w.get("gmgn_buy_30d", 0) or 0
        sell_30d = w.get("gmgn_sell_30d", 0) or 0
        sol_balance = w.get("gmgn_sol_balance", 0) or 0
        is_bot = bool(w.get("is_bot_speed"))

        # Profit score (0-40): $0 → 0pts, $100K+ → 40pts, logarithmic scale
        if profit_30d > 0:
            import math
            profit_score = min(40.0, (math.log10(profit_30d + 1) / math.log10(100001)) * 40)
        else:
            profit_score = 0.0

        # Win rate score (0-25): winrate * 25 (GMGN winrate is 0-1)
        if winrate > 1:
            winrate = winrate / 100  # Handle if given as percentage
        winrate_score = min(25.0, winrate * 25)

        # Consistency score (0-20): based on trades over 30 days
        trades_30d = buy_30d + sell_30d
        consistency_score = min(20.0, (trades_30d / 200) * 20)

        # Balance score (0-10): SOL balance, capped at 50 SOL
        balance_score = min(10.0, (sol_balance / 50) * 10)

        # Bot penalty: -10 if bot
        bot_penalty = -10.0 if is_bot else 0.0

        # Copy performance bonus: +5 if proven winner, -5 if proven loser
        wallet_addr = w.get("address", "")
        wallet_pnl = copy_perf.get(wallet_addr, 0)
        if wallet_pnl > 0:
            copy_bonus = 5.0
        elif wallet_pnl < 0:
            copy_bonus = -5.0
        else:
            copy_bonus = 0.0

        total = profit_score + winrate_score + consistency_score + balance_score + bot_penalty + copy_bonus
        return max(0.0, min(100.0, round(total, 1)))

    async def score_all_wallets(self) -> dict:
        """
        Re-score all wallets in the database using the composite formula.
        Can be called independently of the full refresh cycle.
        Returns summary with score distribution.
        """
        copy_perf = await self.db.get_copy_performance_by_wallet()
        all_wallets = await self.db.get_all_wallets()

        scores = []
        bot_count = 0
        for w in all_wallets:
            score = self._compute_wallet_score(w, copy_perf)
            await self.db.update_wallet_score(w["address"], score)
            scores.append(score)
            if w.get("is_bot_speed"):
                bot_count += 1

        if scores:
            return {
                "wallets_scored": len(scores),
                "avg_score": round(sum(scores) / len(scores), 1),
                "max_score": max(scores),
                "min_score": min(scores),
                "bot_count": bot_count,
                "human_count": len(scores) - bot_count,
            }
        return {"wallets_scored": 0}
