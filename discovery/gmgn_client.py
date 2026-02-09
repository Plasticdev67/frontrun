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

Uses curl_cffi to impersonate Chrome's TLS fingerprint — required to bypass
Cloudflare's bot detection. Also needs cf_clearance + __cf_bm cookies from
browser DevTools (Application → Cookies → gmgn.ai). Cookies expire periodically.
"""

import asyncio
from typing import Any

from curl_cffi import requests as curl_requests

from utils.logger import get_logger

logger = get_logger(__name__)


class GMGNClient:
    """
    Client for the GMGN.ai API using curl_cffi for Cloudflare bypass.

    Uses curl_cffi with Chrome TLS impersonation — this is what makes
    Cloudflare accept our requests even though we're not a real browser.

    Usage:
        client = GMGNClient(cf_clearance="...", cf_bm="...")
        tokens = await client.get_top_tokens("24h")
        buyers = await client.get_top_buyers("token_address_here")
        client.close()
    """

    BASE_URL = "https://gmgn.ai/defi/quotation/v1"

    # Extra headers — curl_cffi handles User-Agent via impersonation
    HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://gmgn.ai/",
        "Origin": "https://gmgn.ai",
    }

    def __init__(self, cf_clearance: str = "", cf_bm: str = "", **_kwargs):
        self.cf_clearance = cf_clearance
        self.cf_bm = cf_bm
        # curl_cffi session with Chrome TLS fingerprint
        self._session = curl_requests.Session(impersonate="chrome")

    def close(self):
        """Clean up the curl_cffi session."""
        if self._session:
            self._session.close()

    @property
    def _cookies(self) -> dict:
        """Build Cloudflare cookies dict."""
        cookies = {}
        if self.cf_clearance:
            cookies["cf_clearance"] = self.cf_clearance
        if self.cf_bm:
            cookies["__cf_bm"] = self.cf_bm
        return cookies

    @property
    def is_authenticated(self) -> bool:
        """Whether we have Cloudflare cookie auth configured."""
        return bool(self.cf_clearance)

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """
        Make a GET request to the GMGN API.

        Uses asyncio.to_thread to run the synchronous curl_cffi request
        in a thread pool — keeps the rest of our async pipeline non-blocking.
        """
        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = await asyncio.to_thread(
                self._session.get,
                url,
                headers=self.HEADERS,
                cookies=self._cookies,
                params=params,
                timeout=15,
            )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                logger.warning("gmgn_rate_limited", endpoint=endpoint)
                await asyncio.sleep(3)
                return await self._get(endpoint, params)
            else:
                logger.debug(
                    "gmgn_blocked",
                    status=response.status_code,
                    endpoint=endpoint,
                    note="Cloudflare — check cookies",
                )
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

    async def get_top_buyers(self, token_address: str) -> list[dict]:
        """
        Get top buyers/holders for a specific token.

        Returns wallet addresses with status (hold/sold) and tags
        (sniper, fresh_wallet, etc). Does NOT include PnL per wallet —
        use get_wallet_stats() for that.

        Args:
            token_address: Solana token mint address

        Returns:
            List of holder dicts with wallet_address, status, tags.
        """
        data = await self._get(f"/tokens/top_buyers/sol/{token_address}")

        if not data:
            return []

        # Response is nested: data.holders.holderInfo[]
        holders = data.get("data", {}).get("holders", {})
        holder_info = holders.get("holderInfo", [])

        if not isinstance(holder_info, list):
            return []

        logger.debug("gmgn_top_buyers_fetched", token=token_address[:8], count=len(holder_info))
        return holder_info

    async def get_wallet_stats(self, wallet_address: str) -> dict:
        """
        Get detailed stats for a specific wallet — PnL, trade frequency, balance.

        This is the key endpoint for wallet scoring. Returns:
        - realized_profit / realized_profit_30d: Dollar profits
        - pnl / pnl_30d / pnl_7d: Return rate (0.05 = 5%)
        - buy_30d / sell_30d: Trade frequency
        - sol_balance: Current SOL balance
        - tags: Smart money labels (e.g., ["smart_degen"])
        - winrate: Win rate (often None for non-tracked wallets)
        - risk: Risk assessment dict

        Args:
            wallet_address: Solana wallet address

        Returns:
            Wallet stats dict, or empty dict if not found.
        """
        data = await self._get(f"/smartmoney/sol/walletNew/{wallet_address}")

        if not data:
            return {}

        stats = data.get("data", {})
        if not isinstance(stats, dict):
            return {}

        return stats

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
