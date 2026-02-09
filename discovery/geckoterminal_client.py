"""
GeckoTerminal Client
====================
Client for the GeckoTerminal API — free, no auth, no Cloudflare.

GeckoTerminal (by CoinGecko) provides:
- Trending pools on Solana with price changes across 6 timeframes
- Top pools by volume
- New pools (recently created)
- Token metadata (address, symbol, name)
- Liquidity (reserve_in_usd) and fully diluted valuation (fdv_usd)
- Transaction counts (buys, sells, buyers, sellers)

Why GeckoTerminal?
- No API key needed
- No Cloudflare protection (unlike GMGN)
- Good data quality — same data that powers CoinGecko
- 30 requests/minute rate limit (generous)
- Returns 20 pools per page, with pagination

API docs: https://www.geckoterminal.com/dex-api
"""

import asyncio
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class GeckoTerminalClient:
    """
    Client for the GeckoTerminal API.

    Usage:
        client = GeckoTerminalClient(session)
        pools = await client.get_trending_pools()
        pools = await client.get_top_pools_by_volume()
        pools = await client.get_new_pools()
    """

    BASE_URL = "https://api.geckoterminal.com/api/v2"

    HEADERS = {
        "Accept": "application/json;version=20230302",
    }

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a GET request to the GeckoTerminal API."""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            async with self.session.get(url, headers=self.HEADERS, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    logger.warning("geckoterminal_rate_limited", endpoint=endpoint)
                    await asyncio.sleep(3)
                    return await self._get(endpoint, params)
                else:
                    error_text = await response.text()
                    logger.error("geckoterminal_error", status=response.status, endpoint=endpoint, error=error_text[:200])
                    return {}
        except Exception as e:
            logger.error("geckoterminal_request_exception", endpoint=endpoint, error=str(e))
            return {}

    async def get_trending_pools(self, pages: int = 3) -> list[dict]:
        """
        Get trending pools on Solana (most active/hot right now).

        Returns pools with price changes, volume, liquidity, and token info.
        Each page returns ~20 pools.
        """
        all_pools = []
        for page in range(1, pages + 1):
            data = await self._get(
                "/networks/solana/trending_pools",
                params={"include": "base_token", "page": str(page)},
            )
            pools = self._extract_pools(data)
            all_pools.extend(pools)

            if not pools:
                break
            await asyncio.sleep(1)  # Rate limit: 30 req/min

        logger.info("geckoterminal_trending_fetched", count=len(all_pools))
        return all_pools

    async def get_top_pools_by_volume(self, pages: int = 3) -> list[dict]:
        """
        Get top Solana pools by 24h trading volume.

        High-volume pools are actively traded — good for finding tokens
        with enough liquidity for our copy trades.
        """
        all_pools = []
        for page in range(1, pages + 1):
            data = await self._get(
                "/networks/solana/pools",
                params={
                    "include": "base_token",
                    "page": str(page),
                    "sort": "h24_volume_usd_desc",
                },
            )
            pools = self._extract_pools(data)
            all_pools.extend(pools)

            if not pools:
                break
            await asyncio.sleep(1)

        logger.info("geckoterminal_volume_fetched", count=len(all_pools))
        return all_pools

    async def get_new_pools(self, pages: int = 2) -> list[dict]:
        """
        Get recently created pools on Solana.

        New pools = early-stage tokens. These are the most alpha-rich
        but also the highest risk. The wallet finder will determine
        which early buyers are legit.
        """
        all_pools = []
        for page in range(1, pages + 1):
            data = await self._get(
                "/networks/solana/new_pools",
                params={"include": "base_token", "page": str(page)},
            )
            pools = self._extract_pools(data)
            all_pools.extend(pools)

            if not pools:
                break
            await asyncio.sleep(1)

        logger.info("geckoterminal_new_pools_fetched", count=len(all_pools))
        return all_pools

    async def get_token_pools(self, token_address: str) -> list[dict]:
        """Get all pools for a specific token."""
        data = await self._get(
            f"/networks/solana/tokens/{token_address}/pools",
            params={"include": "base_token"},
        )
        return self._extract_pools(data)

    def _extract_pools(self, data: dict) -> list[dict]:
        """
        Extract pool data from GeckoTerminal API response.

        Combines the pool attributes with the included token data
        to produce a clean, flat structure.
        """
        if not data:
            return []

        # Build a lookup map for included token data
        token_map = {}
        for included in data.get("included", []):
            if included.get("type") == "token":
                token_id = included.get("id", "")
                attrs = included.get("attributes", {})
                token_map[token_id] = attrs

        pools = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            relationships = item.get("relationships", {})

            # Get the base token info from the included data
            base_token_ref = relationships.get("base_token", {}).get("data", {})
            base_token_id = base_token_ref.get("id", "")
            base_token = token_map.get(base_token_id, {})

            # Extract price change data
            price_changes = attrs.get("price_change_percentage", {}) or {}
            volumes = attrs.get("volume_usd", {}) or {}
            transactions = attrs.get("transactions", {}) or {}

            # Calculate buyer count from transaction data (proxy for holder activity)
            h24_txns = transactions.get("h24", {}) or {}
            total_buyers = h24_txns.get("buyers", 0) or 0
            total_sellers = h24_txns.get("sellers", 0) or 0

            pool = {
                "pool_address": attrs.get("address", ""),
                "pool_name": attrs.get("name", ""),
                "pool_created_at": attrs.get("pool_created_at"),
                # Token data from included
                "token_address": base_token.get("address", ""),
                "token_symbol": base_token.get("symbol", ""),
                "token_name": base_token.get("name", ""),
                # Market data
                "price_usd": float(attrs.get("base_token_price_usd") or 0),
                "fdv_usd": float(attrs.get("fdv_usd") or 0),
                "market_cap_usd": float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0),
                "reserve_usd": float(attrs.get("reserve_in_usd") or 0),
                # Price changes across timeframes
                "price_change_m5": float(price_changes.get("m5") or 0),
                "price_change_m15": float(price_changes.get("m15") or 0),
                "price_change_m30": float(price_changes.get("m30") or 0),
                "price_change_h1": float(price_changes.get("h1") or 0),
                "price_change_h6": float(price_changes.get("h6") or 0),
                "price_change_h24": float(price_changes.get("h24") or 0),
                # Volume
                "volume_h24": float(volumes.get("h24") or 0),
                "volume_h6": float(volumes.get("h6") or 0),
                "volume_h1": float(volumes.get("h1") or 0),
                # Transaction activity
                "buyers_h24": total_buyers,
                "sellers_h24": total_sellers,
                # DEX info
                "dex_id": relationships.get("dex", {}).get("data", {}).get("id", ""),
            }

            # Only include pools where we have the token address
            if pool["token_address"]:
                pools.append(pool)

        return pools
