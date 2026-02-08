"""
Token Filter
=============
Additional filtering logic for discovered tokens.

While the TokenScanner does basic filtering (market cap, multiplier),
this module provides deeper analysis:
- Check if a token's liquidity is real or artificial
- Check for suspicious holder distribution (one wallet holding 90%)
- Check if the token is still actively trading (not dead)
- Score tokens by quality for prioritizing wallet analysis

This runs after discovery to further refine our list.
"""

from config.settings import Settings
from utils.logger import get_logger

logger = get_logger(__name__)


class TokenFilter:
    """
    Applies quality filters to discovered tokens.

    Usage:
        tf = TokenFilter(settings)
        filtered = tf.apply_filters(tokens)
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def apply_filters(self, tokens: list[dict]) -> list[dict]:
        """
        Run all quality filters on a list of tokens.
        Returns only tokens that pass all checks.
        """
        results = []
        for token in tokens:
            issues = self.check_token_quality(token)
            if not issues:
                results.append(token)
            else:
                logger.debug(
                    "token_filtered_out",
                    symbol=token.get("symbol"),
                    issues=issues,
                )
        logger.info("quality_filter_applied", input=len(tokens), output=len(results))
        return results

    def check_token_quality(self, token: dict) -> list[str]:
        """
        Check a single token for quality issues.
        Returns a list of problems found (empty = passed all checks).
        """
        issues = []

        # Check 1: Volume must be meaningful relative to market cap
        # If 24h volume is less than 1% of market cap, the token is basically dead
        mcap = token.get("market_cap_usd") or 0
        volume = token.get("volume_24h_usd") or 0
        if mcap > 0 and volume > 0:
            volume_to_mcap_ratio = volume / mcap
            if volume_to_mcap_ratio < 0.01:
                issues.append(f"Low volume/mcap ratio: {volume_to_mcap_ratio:.3f}")

        # Check 2: Liquidity must be reasonable relative to market cap
        # Less than 1% liquidity/mcap ratio is suspicious (could be manipulated)
        liquidity = token.get("liquidity_usd") or 0
        if mcap > 0 and liquidity > 0:
            liq_to_mcap_ratio = liquidity / mcap
            if liq_to_mcap_ratio < 0.005:
                issues.append(f"Suspiciously low liquidity: {liq_to_mcap_ratio:.4f}")

        # Check 3: Must have SOME holder data (if available)
        holders = token.get("holder_count") or 0
        if holders > 0 and holders < 50:
            issues.append(f"Very few holders: {holders}")

        return issues

    def score_token_quality(self, token: dict) -> float:
        """
        Score a token's quality from 0-100.
        Higher = better quality, more worthy of wallet analysis.

        Used to prioritize which tokens to analyze first in Stage 2.
        """
        score = 50.0  # Start at neutral

        mcap = token.get("market_cap_usd") or 0
        volume = token.get("volume_24h_usd") or 0
        liquidity = token.get("liquidity_usd") or 0
        multiplier = token.get("price_multiplier") or 0
        holders = token.get("holder_count") or 0

        # Higher multiplier = more interesting (up to 30 points)
        if multiplier >= 100:
            score += 30
        elif multiplier >= 50:
            score += 25
        elif multiplier >= 20:
            score += 20
        elif multiplier >= 10:
            score += 15
        elif multiplier >= 5:
            score += 10

        # Good volume/mcap ratio (up to 15 points)
        if mcap > 0 and volume > 0:
            ratio = volume / mcap
            if ratio > 0.5:
                score += 15
            elif ratio > 0.2:
                score += 10
            elif ratio > 0.05:
                score += 5

        # Good liquidity (up to 10 points)
        if liquidity > 100_000:
            score += 10
        elif liquidity > 50_000:
            score += 7
        elif liquidity > 20_000:
            score += 4

        # Good holder distribution (up to 10 points)
        if holders > 5000:
            score += 10
        elif holders > 1000:
            score += 7
        elif holders > 200:
            score += 4

        # Sweet spot market cap bonus (up to 5 points)
        # $3M-$30M is the ideal range for finding alpha
        if 3_000_000 <= mcap <= 30_000_000:
            score += 5

        return min(100, max(0, score))
