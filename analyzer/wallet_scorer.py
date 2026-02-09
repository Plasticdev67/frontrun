"""
Wallet Scorer
=============
Scores and ranks wallets by how "smart" their trading behavior is.

The scoring system evaluates wallets across 4 dimensions:

1. PnL Score (0-25 points)
   - How much profit has this wallet made overall?
   - Higher total PnL = higher score

2. Win Rate Score (0-25 points)
   - What percentage of their trades are profitable?
   - 80%+ win rate is exceptional, 50%+ is decent

3. Timing Score (0-25 points)
   - How early do they typically buy winning tokens?
   - Being in the first 100 buyers is great, first 500 is decent

4. Consistency Score (0-25 points)
   - Are they a one-hit wonder or consistently profitable?
   - Profitable across multiple different tokens = consistent

Total score is 0-100. Wallets above 60 are considered "smart money"
worth monitoring.

Why this scoring system?
- PnL alone isn't enough (could be one lucky trade)
- Win rate alone isn't enough (could be tiny profits)
- Timing alone isn't enough (could be a bot that always loses)
- Consistency ties it all together — we want wallets that REPEATEDLY find winners
"""

from config.settings import Settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)


class WalletScorer:
    """
    Scores wallets based on their trading performance across multiple tokens.

    Usage:
        scorer = WalletScorer(settings, db)
        scores = await scorer.score_wallets(wallet_data)
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    async def score_wallets(self, wallet_data: dict[str, list[dict]]) -> list[dict]:
        """
        Score all wallet candidates and save results to the database.

        Args:
            wallet_data: From WalletFinder — maps wallet_address to their trades

        Returns:
            List of scored wallets, sorted by total_score (best first)
        """
        logger.info("scoring_wallets", count=len(wallet_data))
        scored_wallets = []

        for address, trades in wallet_data.items():
            score = self._calculate_score(address, trades)

            # Save to database
            await self.db.upsert_wallet(score)
            scored_wallets.append(score)

        # Sort by total score (highest first)
        scored_wallets.sort(key=lambda w: w["total_score"], reverse=True)

        # Log the top wallets
        self._print_leaderboard(scored_wallets[:20])

        logger.info(
            "scoring_complete",
            total_scored=len(scored_wallets),
            above_threshold=sum(
                1 for w in scored_wallets
                if w["total_score"] >= self.settings.min_wallet_score
            ),
        )

        return scored_wallets

    def _calculate_score(self, address: str, trades: list[dict]) -> dict:
        """
        Calculate the full score breakdown for a single wallet.

        Returns a dictionary with the score and all raw stats,
        ready to be saved to the database.
        """
        # Extract raw stats from trade data
        total_pnl = sum(t.get("pnl_sol", 0) or 0 for t in trades)
        total_trades = len(trades)

        winning_trades = sum(1 for t in trades if (t.get("pnl_sol") or 0) > 0)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0

        # Average entry rank (lower = earlier = better)
        entry_ranks = [t.get("entry_rank", 500) for t in trades if t.get("entry_rank")]
        avg_entry_rank = sum(entry_ranks) / len(entry_ranks) if entry_ranks else 500

        # Count unique winning tokens (consistency check)
        unique_winners = len(set(
            t.get("token_mint") for t in trades
            if (t.get("pnl_sol") or 0) > 0 and t.get("token_mint")
        ))

        # How many different tokens total (not just winners)
        unique_tokens = len(set(t.get("token_mint") for t in trades if t.get("token_mint")))

        # GMGN enrichment data (from walletNew endpoint)
        # Use the first trade's GMGN data — it's the same for all trades from this wallet
        # GMGN sometimes returns strings instead of numbers — safe-convert everything
        def _float(val, default=0):
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        gmgn_profit = _float(trades[0].get("gmgn_realized_profit"))
        gmgn_profit_30d = _float(trades[0].get("gmgn_realized_profit_30d"))
        gmgn_pnl_30d = _float(trades[0].get("gmgn_pnl_30d"))
        gmgn_buy_30d = int(_float(trades[0].get("gmgn_buy_30d")))
        gmgn_sol_balance = _float(trades[0].get("gmgn_sol_balance"))
        gmgn_winrate = trades[0].get("gmgn_winrate")
        gmgn_tags = trades[0].get("gmgn_tags") or []

        # If GMGN has profit data, use it (it's far more accurate than our per-token tracking)
        # GMGN profit is in USD — convert roughly: $150/SOL (approximate)
        effective_pnl = total_pnl
        if gmgn_profit_30d > 0:
            effective_pnl = gmgn_profit_30d / 150  # Approximate SOL equivalent
        elif gmgn_profit > 0:
            effective_pnl = gmgn_profit / 150

        # If GMGN has win rate, use it (GMGN winrate is 0-1 decimal)
        effective_win_rate = win_rate
        effective_total_trades = total_trades
        gmgn_sell_30d = int(_float(trades[0].get("gmgn_sell_30d")))
        if gmgn_winrate is not None:
            try:
                effective_win_rate = float(gmgn_winrate)
                winning_trades = int(effective_win_rate * gmgn_buy_30d) if gmgn_buy_30d else winning_trades
            except (ValueError, TypeError):
                pass
        # If GMGN shows higher trade volume, use it — but cap at a human-realistic level
        # >15,000 buys in 30d = 500/day = clearly a bot, don't inflate our trade count
        if gmgn_buy_30d > effective_total_trades and gmgn_buy_30d <= 15_000:
            effective_total_trades = gmgn_buy_30d

        # Store win rate as a 0-100 percentage for display
        win_rate_pct = round(effective_win_rate * 100, 1) if effective_win_rate <= 1 else round(effective_win_rate, 1)

        # Calculate individual dimension scores
        pnl_score = self._score_pnl(effective_pnl)
        win_rate_score = self._score_win_rate(effective_win_rate, effective_total_trades)
        timing_score = self._score_timing(avg_entry_rank)
        consistency_score = self._score_consistency(unique_winners, unique_tokens)

        total_score = pnl_score + win_rate_score + timing_score + consistency_score

        return {
            "address": address,
            "total_score": round(total_score, 1),
            "pnl_score": round(pnl_score, 1),
            "win_rate_score": round(win_rate_score, 1),
            "timing_score": round(timing_score, 1),
            "consistency_score": round(consistency_score, 1),
            "total_pnl_sol": round(effective_pnl, 4),
            "total_trades": effective_total_trades,
            "winning_trades": winning_trades,
            "win_rate": win_rate_pct,
            "avg_entry_rank": int(avg_entry_rank),
            "unique_winners": unique_winners,
            # GMGN enrichment — stored in DB for dashboard display
            "gmgn_realized_profit_usd": round(gmgn_profit, 2),
            "gmgn_profit_30d_usd": round(gmgn_profit_30d, 2),
            "gmgn_sol_balance": round(gmgn_sol_balance, 2),
            "gmgn_winrate": _float(gmgn_winrate) if gmgn_winrate is not None else None,
            "gmgn_buy_30d": gmgn_buy_30d,
            "gmgn_sell_30d": gmgn_sell_30d,
            "gmgn_tags": gmgn_tags,
            "is_flagged": False,
            "flag_reason": None,
            "is_monitored": False,
        }

    def _score_pnl(self, total_pnl: float) -> float:
        """
        Score based on total profit (0-25 points).

        Scale:
        - 100+ SOL profit = 25 points (whale-level alpha)
        - 50+ SOL = 22 points
        - 20+ SOL = 18 points
        - 10+ SOL = 15 points
        - 5+ SOL = 12 points
        - 1+ SOL = 8 points
        - 0+ SOL = 3 points
        - Negative = 0 points
        """
        if total_pnl <= 0:
            return 0
        elif total_pnl >= 100:
            return 25
        elif total_pnl >= 50:
            return 22
        elif total_pnl >= 20:
            return 18
        elif total_pnl >= 10:
            return 15
        elif total_pnl >= 5:
            return 12
        elif total_pnl >= 1:
            return 8
        else:
            return 3

    def _score_win_rate(self, win_rate: float, total_trades: int) -> float:
        """
        Score based on win rate (0-25 points).

        We also consider the number of trades — a 100% win rate with
        1 trade is meaningless. We need statistical significance.

        Scale (for wallets with 3+ trades):
        - 80%+ win rate = 25 points
        - 70%+ = 20 points
        - 60%+ = 15 points
        - 50%+ = 10 points
        - Below 50% = 5 points
        """
        # Need at least 3 trades for the win rate to be meaningful
        if total_trades < 3:
            # Give partial credit — 2 trades with wins is promising but unproven
            if total_trades >= 2 and win_rate > 0.5:
                return 10
            return 5

        if win_rate >= 0.8:
            return 25
        elif win_rate >= 0.7:
            return 20
        elif win_rate >= 0.6:
            return 15
        elif win_rate >= 0.5:
            return 10
        else:
            return 5

    def _score_timing(self, avg_entry_rank: int) -> float:
        """
        Score based on how early they typically buy (0-25 points).

        Entry rank = position among all buyers (rank 1 = first buyer ever).
        Lower rank = earlier = better alpha.

        Scale:
        - Top 50 buyers on average = 25 points (incredibly early)
        - Top 100 = 22 points
        - Top 200 = 18 points
        - Top 500 = 12 points
        - Top 1000 = 8 points
        - Later than 1000 = 3 points
        """
        if avg_entry_rank <= 50:
            return 25
        elif avg_entry_rank <= 100:
            return 22
        elif avg_entry_rank <= 200:
            return 18
        elif avg_entry_rank <= 500:
            return 12
        elif avg_entry_rank <= 1000:
            return 8
        else:
            return 3

    def _score_consistency(self, unique_winners: int, unique_tokens: int) -> float:
        """
        Score based on consistency across multiple tokens (0-25 points).

        A wallet that's profitable on 5+ different tokens is way more
        trustworthy than one that got lucky on a single token.

        Scale:
        - 10+ unique winners = 25 points
        - 7+ = 22 points
        - 5+ = 18 points
        - 3+ = 14 points
        - 2+ = 10 points
        - 1 = 5 points
        """
        if unique_winners >= 10:
            return 25
        elif unique_winners >= 7:
            return 22
        elif unique_winners >= 5:
            return 18
        elif unique_winners >= 3:
            return 14
        elif unique_winners >= 2:
            return 10
        else:
            return 5

    def _print_leaderboard(self, wallets: list[dict]) -> None:
        """Print the top-scored wallets in a readable format."""
        if not wallets:
            return

        logger.info("=" * 80)
        logger.info("SMART WALLET LEADERBOARD")
        logger.info("=" * 80)

        for i, wallet in enumerate(wallets, 1):
            gmgn_profit = wallet.get("gmgn_profit_30d_usd", 0)
            gmgn_tags = wallet.get("gmgn_tags", [])
            tag_str = ",".join(gmgn_tags[:3]) if gmgn_tags else ""

            logger.info(
                f"#{i}",
                address=wallet["address"][:8] + "..." + wallet["address"][-4:],
                total_score=f"{wallet['total_score']}/100",
                pnl=f"{wallet['total_pnl_sol']:.2f} SOL",
                gmgn_profit=f"${gmgn_profit:,.0f}" if gmgn_profit else "-",
                win_rate=f"{wallet['winning_trades']}/{wallet['total_trades']}",
                avg_rank=wallet["avg_entry_rank"],
                winners=wallet["unique_winners"],
                tags=tag_str or "-",
            )

        logger.info("=" * 80)

    async def auto_select_monitored_wallets(self, scored_wallets: list[dict]) -> list[dict]:
        """
        Automatically select the best wallets for monitoring.

        Takes the top wallets that meet our minimum score threshold
        and marks them as monitored in the database.
        """
        selected = []
        for wallet in scored_wallets:
            if wallet["total_score"] < self.settings.min_wallet_score:
                break  # List is sorted, so all remaining are below threshold
            if len(selected) >= self.settings.max_monitored_wallets:
                break

            # Mark as monitored in database
            wallet["is_monitored"] = True
            await self.db.upsert_wallet(wallet)
            selected.append(wallet)

        logger.info(
            "monitoring_wallets_selected",
            count=len(selected),
            min_score=self.settings.min_wallet_score,
            top_score=selected[0]["total_score"] if selected else 0,
        )

        return selected
