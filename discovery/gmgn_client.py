"""
GMGN Client
============
Client for the GMGN.ai API — our primary source for token discovery and wallet data.

GMGN aggregates Solana token data with built-in analytics:
- Top tokens ranked by price change (pre-calculated, no enrichment needed)
- Safety metrics: rug_ratio, is_wash_trading, bundler_rate
- Smart money signals: smart_degen_count, renowned_count
- Top buyers per token with profit data (replaces Birdeye top traders)

Why GMGN over Birdeye for discovery?
- One API call returns 100 tokens with all data we need (vs 100+ calls on Birdeye)
- Pre-calculated price multipliers (no separate price history lookups)
- Built-in rug detection fields (rug_ratio, wash trading, bundler rate)
- No API key or rate limits (just needs browser-like headers)
- Top buyers endpoint gives us wallet PnL per token

No API key needed — just browser-like headers.
"""

import asyncio
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class GMGNClient:
    """
    Client for the GMGN.ai API.

    Usage:
        client = GMGNClient(session)
        tokens = await client.get_top_tokens("24h")
        buyers = await client.get_top_buyers("token_address_here")
    """

    BASE_URL = "https://gmgn.ai/defi/quotation/v1"

    # Browser-like headers required by GMGN
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://gmgn.ai/",
        "Origin": "https://gmgn.ai",
    }

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a GET request to the GMGN API."""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            async with self.session.get(url, headers=self.HEADERS, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                elif response.status == 429:
                    logger.warning("gmgn_rate_limited", endpoint=endpoint)
                    await asyncio.sleep(3)
                    return await self._get(endpoint, params)
                else:
                    # GMGN is Cloudflare-protected — 403 is expected without cookie auth
                    # Log at debug to avoid spamming logs
                    logger.debug("gmgn_blocked", status=response.status, endpoint=endpoint, note="Cloudflare — needs cookie auth")
                    return {}
        except Exception as e:
            logger.error("gmgn_request_exception", endpoint=endpoint, error=str(e))
            return {}

    async def get_top_tokens(
        self,
        timeframe: str = "24h",
        order_by: str = "price_change_percent",
        direction: str = "desc",
        min_marketcap: float | None = None,
        min_liquidity: float | None = None,
        min_holder_count: int | None = None,
        filters: list[str] | None = None,
    ) -> list[dict]:
        """
        Get top-performing tokens ranked by price change.

        Args:
            timeframe: "5m", "1h", "6h", "24h"
            order_by: Sort field — "price_change_percent", "swaps", "volume"
            direction: "asc" or "desc"
            min_marketcap: Server-side min market cap filter
            min_liquidity: Server-side min liquidity filter
            min_holder_count: Server-side min holder count filter
            filters: List of server-side filters like "renounced_mint"

        Returns:
            List of token dicts with price data and safety metrics.
        """
        params = {
            "orderby": order_by,
            "direction": direction,
        }
        if min_marketcap is not None:
            params["min_marketcap"] = int(min_marketcap)
        if min_liquidity is not None:
            params["min_liquidity"] = int(min_liquidity)
        if min_holder_count is not None:
            params["min_holder_count"] = int(min_holder_count)
        if filters:
            params["filter"] = ",".join(filters)

        data = await self._get(f"/rank/sol/swaps/{timeframe}", params)

        if not data:
            return []

        # GMGN wraps results in data.rank
        rank_data = data.get("data", {}).get("rank", [])
        if not rank_data:
            # Some endpoints use different nesting
            rank_data = data.get("data", [])

        logger.info("gmgn_tokens_fetched", timeframe=timeframe, count=len(rank_data))
        return rank_data if isinstance(rank_data, list) else []

    async def get_top_buyers(
        self,
        token_address: str,
        order_by: str = "profit",
        direction: str = "desc",
    ) -> list[dict]:
        """
        Get top buyers for a specific token, with their profit data.

        This replaces Birdeye's top traders endpoint — GMGN returns
        wallet addresses along with their PnL for this token.

        Args:
            token_address: Solana token mint address
            order_by: "profit", "bought", "sold"
            direction: "asc" or "desc"

        Returns:
            List of buyer dicts with wallet address and profit data.
        """
        params = {
            "orderby": order_by,
            "direction": direction,
        }

        data = await self._get(f"/tokens/top_buyers/sol/{token_address}", params)

        if not data:
            return []

        buyers = data.get("data", [])
        if not isinstance(buyers, list):
            buyers = []

        logger.debug("gmgn_top_buyers_fetched", token=token_address[:8], count=len(buyers))
        return buyers

    async def get_token_info(self, token_address: str) -> dict:
        """
        Get detailed info for a single token.

        Args:
            token_address: Solana token mint address

        Returns:
            Token info dict with price, market cap, safety data, etc.
        """
        data = await self._get(f"/tokens/sol/{token_address}")
        return data.get("data", {}) if data else {}
