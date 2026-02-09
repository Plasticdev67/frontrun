"""
Platform Scraper
================
Fetches top trader wallet addresses from Solana trading platforms.

These wallets are used as ADDITIONAL seed wallets for cluster detection.
The more seed wallets we have, the more clusters we can discover.

Currently supported:
- Bitquery GraphQL API — provides top DEX traders by volume

Unsupported (no public API):
- FOMO, Photon — no public API
- Axiom — limited access, skip for v1

Usage:
    This is a side feature. It runs as part of --clusters if
    BITQUERY_API_KEY is set in .env. Otherwise it's skipped.
"""

import asyncio
from typing import Any

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)

# Bitquery GraphQL endpoint
BITQUERY_URL = "https://streaming.bitquery.io/graphql"

# GraphQL query to get top Solana DEX traders
BITQUERY_TOP_TRADERS_QUERY = """
{
  Solana {
    DEXTradeByTokens(
      orderBy: {descendingByField: "volume"}
      limit: {count: %d}
      where: {
        Trade: {
          Currency: {
            MintAddress: {notIn: [
              "So11111111111111111111111111111111111111112",
              "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            ]}
          }
        }
        Block: {Time: {after: "%s"}}
      }
    ) {
      Trade {
        Account {
          Address
        }
      }
      volume: sum(of: Trade_AmountInUSD)
      trades: count
    }
  }
}
"""


class PlatformScraper:
    """
    Fetches top trader addresses from trading platform leaderboards.

    These are additional seed wallets for cluster detection —
    the more public wallets we start with, the more side wallets we find.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        self.session = aiohttp.ClientSession()
        logger.info("platform_scraper_initialized")

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def fetch_top_traders(self, limit: int = 100) -> list[dict]:
        """
        Fetch top traders from all available platforms.
        Returns list of {"address": str, "source": str, ...}
        """
        all_traders = []

        # Bitquery (if API key available)
        api_key = getattr(self.settings, "bitquery_api_key", "")
        if api_key:
            try:
                bitquery_traders = await self._fetch_bitquery_top_traders(limit)
                all_traders.extend(bitquery_traders)
                logger.info("bitquery_traders_fetched", count=len(bitquery_traders))
            except Exception as e:
                logger.warning("bitquery_fetch_failed", error=str(e))
        else:
            logger.debug("bitquery_skipped", reason="No BITQUERY_API_KEY set")

        # Save to database
        if all_traders:
            saved = await self._save_trader_wallets(all_traders)
            logger.info("platform_wallets_saved", count=saved)

        return all_traders

    async def _fetch_bitquery_top_traders(self, limit: int = 100) -> list[dict]:
        """
        Fetch top Solana DEX traders from Bitquery's GraphQL API.
        Returns wallet addresses with volume and trade count.
        """
        from datetime import datetime, timedelta, timezone

        # Look back 30 days
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = BITQUERY_TOP_TRADERS_QUERY % (limit, since)

        api_key = getattr(self.settings, "bitquery_api_key", "")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        async with self.session.post(
            BITQUERY_URL,
            json={"query": query},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning("bitquery_error", status=resp.status, body=text[:200])
                return []

            data = await resp.json()

        # Parse response
        traders = []
        trades_data = (
            data.get("data", {})
            .get("Solana", {})
            .get("DEXTradeByTokens", [])
        )

        seen = set()
        for entry in trades_data:
            address = entry.get("Trade", {}).get("Account", {}).get("Address", "")
            if not address or address in seen:
                continue
            seen.add(address)

            traders.append({
                "address": address,
                "source": "bitquery",
                "volume_usd": float(entry.get("volume", 0) or 0),
                "trade_count": int(entry.get("trades", 0) or 0),
            })

        return traders

    async def _save_trader_wallets(self, traders: list[dict]) -> int:
        """
        Save discovered trader wallets to the wallets table.
        Does NOT set is_monitored — that happens after scoring/clustering.
        """
        count = 0
        for trader in traders:
            try:
                await self.db.upsert_wallet({
                    "address": trader["address"],
                    "is_monitored": False,
                })
                count += 1
            except Exception as e:
                logger.debug("wallet_save_failed", address=trader["address"][:8], error=str(e))

        return count
