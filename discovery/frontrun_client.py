"""
Frontrun Pro Client
====================
Integration with Frontrun Pro (frontrun.pro) — a Chrome extension and analytics
platform that tracks top Solana traders, resolves wallet addresses, and detects
alt wallets.

Key features we use:
- Address resolution: turn truncated wallet snippets into full addresses
- Top trader wallet discovery: find wallets from FOMO leaderboard

Frontrun Pro has no public API, so we use their address finder web endpoint
with curl_cffi for Cloudflare bypass. For bulk wallet imports we provide
manual batch-add functionality that pairs with GMGN enrichment.

Usage:
    client = FrontrunClient()
    full_address = await client.resolve_address("Abhxa...YsuS")
    client.close()
"""

import asyncio
from typing import Any

from curl_cffi import requests as curl_requests

from utils.logger import get_logger

logger = get_logger(__name__)


class FrontrunClient:
    """
    Client for Frontrun Pro wallet resolution and discovery.

    Uses curl_cffi with Chrome TLS impersonation for Cloudflare bypass.
    """

    BASE_URL = "https://www.frontrun.pro"

    HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.frontrun.pro/address-finder",
        "Origin": "https://www.frontrun.pro",
    }

    def __init__(self):
        self._session = curl_requests.Session(impersonate="chrome")

    def close(self):
        """Clean up the curl_cffi session."""
        if self._session:
            self._session.close()

    async def _get(self, url: str, params: dict | None = None) -> dict:
        """Make a GET request with Chrome TLS fingerprint."""
        try:
            response = await asyncio.to_thread(
                self._session.get,
                url,
                headers=self.HEADERS,
                params=params,
                timeout=15,
            )
            if response.status_code == 200:
                try:
                    return response.json()
                except Exception:
                    return {"html": response.text}
            else:
                logger.debug(
                    "frontrun_request_failed",
                    status=response.status_code,
                    url=url,
                )
                return {}
        except Exception as e:
            logger.error("frontrun_request_error", url=url, error=str(e))
            return {}

    async def _post(self, url: str, json_data: dict | None = None) -> dict:
        """Make a POST request with Chrome TLS fingerprint."""
        try:
            response = await asyncio.to_thread(
                self._session.post,
                url,
                headers={**self.HEADERS, "Content-Type": "application/json"},
                json=json_data,
                timeout=15,
            )
            if response.status_code == 200:
                try:
                    return response.json()
                except Exception:
                    return {"html": response.text}
            else:
                logger.debug(
                    "frontrun_post_failed",
                    status=response.status_code,
                    url=url,
                )
                return {}
        except Exception as e:
            logger.error("frontrun_post_error", url=url, error=str(e))
            return {}

    async def resolve_address(self, partial_address: str) -> str | None:
        """
        Resolve a truncated wallet address to its full Solana address.

        Uses Frontrun Pro's address finder. The input can be:
        - A truncated address like "Abhxa...YsuS"
        - A partial start/end snippet
        - A Twitter @handle

        Returns the full address, or None if not found.
        """
        logger.info("frontrun_resolving", partial=partial_address)

        # Try the address finder API endpoint
        data = await self._post(
            f"{self.BASE_URL}/api/address-finder",
            json_data={"query": partial_address},
        )

        if data and not data.get("html"):
            # Check common response structures
            address = (
                data.get("address")
                or data.get("wallet_address")
                or data.get("result", {}).get("address")
                or data.get("data", {}).get("address")
            )
            if address and len(address) >= 32:
                logger.info("frontrun_resolved", partial=partial_address, full=address[:8])
                return address

        # Try alternative endpoint
        data = await self._get(
            f"{self.BASE_URL}/api/search",
            params={"q": partial_address},
        )

        if data and not data.get("html"):
            results = data.get("results", []) or data.get("data", [])
            if isinstance(results, list) and results:
                address = results[0].get("address") or results[0].get("wallet_address")
                if address and len(address) >= 32:
                    logger.info("frontrun_resolved", partial=partial_address, full=address[:8])
                    return address

        logger.warning("frontrun_resolve_failed", partial=partial_address)
        return None

    async def batch_resolve(self, partials: list[str]) -> dict[str, str | None]:
        """
        Resolve multiple truncated addresses.

        Returns a dict mapping partial → full address (or None if not found).
        """
        results = {}
        for partial in partials:
            full = await self.resolve_address(partial)
            results[partial] = full
            await asyncio.sleep(1)  # Rate limit
        return results

    @staticmethod
    def parse_fomo_leaderboard(raw_wallets: list[dict]) -> list[dict]:
        """
        Parse raw FOMO leaderboard data into standardized format.

        Input: list of dicts with whatever fields we can scrape/screenshot
        Output: list of standardized trader dicts ready for DB insertion

        This is a helper for manual data entry from screenshots.
        """
        traders = []
        for i, w in enumerate(raw_wallets):
            trader = {
                "wallet_address": w.get("address", ""),
                "username": w.get("username", ""),
                "twitter_handle": w.get("twitter", ""),
                "platform": w.get("platform", "fomo"),
                "ranking": w.get("rank", i + 1),
                "pnl_24h_usd": w.get("pnl_24h", 0),
                "pnl_7d_usd": w.get("pnl_7d", 0),
                "pnl_30d_usd": w.get("pnl_30d", 0),
                "pnl_all_time_usd": w.get("pnl_all_time", 0),
            }
            if trader["wallet_address"]:
                traders.append(trader)
        return traders
