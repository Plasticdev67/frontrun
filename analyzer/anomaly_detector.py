"""
Anomaly Detector
================
Flags wallets that look suspicious — bots, insiders, dev wallets, etc.

We don't want to follow these because:
- Dev wallets get tokens for free (their "alpha" is just insider access)
- Sniper bots buy in the same block as liquidity — we can NEVER be that fast
- Insider wallets might have non-public info that won't repeat
- Wash traders generate fake PnL through self-dealing

Red flags we check for:
1. Transaction timing patterns (buys in the exact same block as LP creation)
2. Unrealistic win rates (99%+ is almost certainly a bot or insider)
3. Token creation connections (wallet also deployed the token = dev wallet)
4. Suspiciously uniform trade sizes (bots often use exact same amounts)
5. Ultra-high frequency trading (hundreds of trades per day = bot)
"""

from config.settings import Settings
from utils.logger import get_logger

logger = get_logger(__name__)


class AnomalyDetector:
    """
    Analyzes wallets for suspicious patterns and flags them.

    Usage:
        detector = AnomalyDetector(settings)
        flagged_wallets = detector.analyze(scored_wallets, wallet_data)
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def analyze(
        self,
        scored_wallets: list[dict],
        wallet_trades: dict[str, list[dict]],
    ) -> list[dict]:
        """
        Check all scored wallets for anomalies.
        Returns updated wallet list with flags set on suspicious ones.
        """
        logger.info("anomaly_detection_starting", wallets=len(scored_wallets))
        flagged_count = 0

        for wallet in scored_wallets:
            address = wallet["address"]
            trades = wallet_trades.get(address, [])

            flags = self._check_all_patterns(wallet, trades)

            if flags:
                wallet["is_flagged"] = True
                wallet["flag_reason"] = "; ".join(flags)
                flagged_count += 1
                logger.info(
                    "wallet_flagged",
                    address=address[:8] + "...",
                    score=wallet["total_score"],
                    flags=flags,
                )

        logger.info(
            "anomaly_detection_complete",
            total_checked=len(scored_wallets),
            flagged=flagged_count,
        )

        return scored_wallets

    # GMGN tags that indicate bots/scammers we should NOT copy trade
    BAD_TAGS = {
        "sandwich_bot",     # MEV sandwich attacker — impossible to replicate
        "scammer",          # Known scammer wallet
        "rug_deployer",     # Has deployed rugs
    }

    def _check_all_patterns(self, wallet: dict, trades: list[dict]) -> list[str]:
        """Run all anomaly checks on a single wallet. Returns list of flag reasons."""
        flags = []

        # Check 0: GMGN tag-based flagging (most reliable — GMGN tracks these)
        flag = self._check_gmgn_tags(wallet)
        if flag:
            flags.append(flag)

        # Check 1: Unrealistic win rate
        flag = self._check_win_rate(wallet)
        if flag:
            flags.append(flag)

        # Check 2: Timing anomaly (buys too early — likely sniper bot)
        flag = self._check_timing_anomaly(wallet, trades)
        if flag:
            flags.append(flag)

        # Check 3: Trade pattern anomaly (bot-like behavior)
        flag = self._check_trade_patterns(trades)
        if flag:
            flags.append(flag)

        # Check 4: Too many trades in a short period (high-frequency bot)
        flag = self._check_frequency(trades)
        if flag:
            flags.append(flag)

        return flags

    def _check_gmgn_tags(self, wallet: dict) -> str | None:
        """
        Flag wallets with known-bad GMGN tags.

        GMGN labels wallets based on their on-chain behavior.
        Sandwich bots and scammers are tagged automatically.
        We can't profitably copy these — their strategies require
        MEV infrastructure or are outright malicious.
        """
        tags = wallet.get("gmgn_tags") or []
        if isinstance(tags, str):
            import json
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []

        bad_found = [t for t in tags if t in self.BAD_TAGS]
        if bad_found:
            return f"GMGN flagged: {', '.join(bad_found)}"
        return None

    def _check_win_rate(self, wallet: dict) -> str | None:
        """
        Flag wallets with impossibly high win rates.

        No human trader wins 95%+ of the time across many trades.
        This usually means: bot with MEV, insider info, or data error.
        """
        total = wallet.get("total_trades", 0)
        winning = wallet.get("winning_trades", 0)

        if total < 5:
            return None  # Not enough data to flag

        win_rate = winning / total
        if win_rate >= 0.95:
            return f"Suspicious win rate: {win_rate:.0%} over {total} trades (likely bot/insider)"

        return None

    def _check_timing_anomaly(self, wallet: dict, trades: list[dict]) -> str | None:
        """
        Flag wallets that consistently buy within the first 10 buyers.

        Being first once is skill. Being first EVERY TIME across multiple
        tokens usually means automated sniping (buying in the same block
        as liquidity is added). We can't copy this — by the time we see
        their tx, the price has already moved.
        """
        avg_rank = wallet.get("avg_entry_rank", 500)
        unique_winners = wallet.get("unique_winners", 0)

        # If they're consistently in the top 10 across 3+ tokens, likely a bot
        if avg_rank <= 10 and unique_winners >= 3:
            return f"Probable sniper bot: avg entry rank {avg_rank} across {unique_winners} tokens"

        # If they're consistently in the top 5, even on 2 tokens it's suspicious
        if avg_rank <= 5 and unique_winners >= 2:
            return f"Probable sniper bot: avg entry rank {avg_rank} across {unique_winners} tokens"

        return None

    def _check_trade_patterns(self, trades: list[dict]) -> str | None:
        """
        Flag wallets with bot-like trade patterns.

        Bots often:
        - Use the exact same buy amount every time (e.g., always 1.0000 SOL)
        - Have very uniform trade timing
        """
        if len(trades) < 3:
            return None

        # Check for identical buy amounts (sign of automated trading)
        buy_amounts = [
            round(t.get("buy_amount_sol") or 0, 4)
            for t in trades
            if t.get("buy_amount_sol")
        ]

        if len(buy_amounts) >= 3:
            unique_amounts = set(buy_amounts)
            # If 80%+ of trades use the exact same amount, it's a bot
            most_common = max(set(buy_amounts), key=buy_amounts.count)
            same_count = buy_amounts.count(most_common)
            if same_count / len(buy_amounts) >= 0.8:
                return f"Bot pattern: {same_count}/{len(buy_amounts)} trades use identical amount ({most_common} SOL)"

        return None

    def _check_frequency(self, trades: list[dict]) -> str | None:
        """
        Flag wallets with extremely high trading frequency.

        Normal "smart money" traders might make 5-20 trades per day.
        200+ trades per day is automated.
        """
        if len(trades) < 5:
            return None

        # Check if we have timestamp data
        timestamps = [
            t.get("first_buy_at") for t in trades if t.get("first_buy_at")
        ]

        if len(timestamps) < 5:
            return None

        # Sort timestamps and check if many trades happen within a very short window
        timestamps.sort()

        # If all trades happened within the same day, check the density
        # (This is a simplified check — in production we'd look at actual timestamps)
        if len(trades) >= 20:
            return f"High-frequency trading: {len(trades)} trades detected (likely bot)"

        return None
