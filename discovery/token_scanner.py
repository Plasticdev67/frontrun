"""
Token Scanner
=============
Finds the best-performing Solana tokens using multiple data sources.

Strategy (v2 — GMGN primary):
1. Pull top tokens from GMGN.ai (PRIMARY — pre-ranked with safety data, 1 call)
2. Pull trending tokens from DexScreener (SECONDARY — catches what GMGN misses)
3. Birdeye available as FALLBACK for enrichment if needed
4. Merge, deduplicate, and filter
5. Store everything in the database

Why GMGN as primary?
- One API call returns 100 tokens pre-sorted by price performance
- Built-in safety metrics (rug_ratio, wash trading, bundler rate)
- No API key needed, no rate limit issues
- Birdeye's free tier gets exhausted after ~100 price history lookups

This module runs on-demand (not continuously). You trigger it when you want
to refresh your token list.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp

from config.settings import Settings
from database.db import Database
from discovery.gmgn_client import GMGNClient
from discovery.geckoterminal_client import GeckoTerminalClient
from utils.logger import get_logger

logger = get_logger(__name__)


class BirdeyeClient:
    """
    Client for the Birdeye API — our primary source for token data.

    Birdeye tracks every token on Solana and provides:
    - Price history (OHLCV candles)
    - Market cap, volume, liquidity
    - Top traders for each token
    - Token metadata (name, symbol, logo)

    API docs: https://docs.birdeye.so/
    """

    BASE_URL = "https://public-api.birdeye.so"

    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session
        self.headers = {
            "X-API-KEY": api_key,
            "x-chain": "solana",
        }

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a GET request to the Birdeye API."""
        url = f"{self.BASE_URL}{endpoint}"
        async with self.session.get(url, headers=self.headers, params=params) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 429:
                # Rate limited — wait and retry
                logger.warning("birdeye_rate_limited", endpoint=endpoint)
                await asyncio.sleep(2)
                return await self._get(endpoint, params)
            else:
                error_text = await response.text()
                logger.error("birdeye_error", status=response.status, endpoint=endpoint, error=error_text)
                return {}

    async def get_top_gainers(self, time_range: str = "24h", limit: int = 50) -> list[dict]:
        """
        Get tokens with the highest price gains.

        Args:
            time_range: "1h", "4h", "12h", "24h"
            limit: How many tokens to return
        """
        params = {
            "sort_by": "price_change_24h_percent",
            "sort_type": "desc",
            "offset": 0,
            "limit": limit,
        }
        data = await self._get("/defi/token_trending", params)
        return data.get("data", {}).get("items", [])

    async def get_token_list(
        self,
        sort_by: str = "mc",
        sort_type: str = "desc",
        min_market_cap: float = 1_000_000,
        max_market_cap: float = 50_000_000,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Get a filtered list of Solana tokens.

        Args:
            sort_by: What to sort by ("mc" = market cap, "v24hUSD" = 24h volume)
            sort_type: "asc" or "desc"
            min_market_cap: Minimum market cap filter
            max_market_cap: Maximum market cap filter
            limit: Number of results per page
            offset: Pagination offset
        """
        params = {
            "sort_by": sort_by,
            "sort_type": sort_type,
            "offset": offset,
            "limit": limit,
            "min_market_cap": min_market_cap,
            "max_market_cap": max_market_cap,
        }
        data = await self._get("/defi/tokenlist", params)
        return data.get("data", {}).get("tokens", [])

    async def get_token_overview(self, token_address: str) -> dict:
        """
        Get detailed info about a specific token.
        Returns: price, market cap, volume, liquidity, holder count, etc.
        """
        params = {"address": token_address}
        data = await self._get("/defi/token_overview", params)
        return data.get("data", {})

    async def get_token_price_history(
        self, token_address: str, interval: str = "1D", time_from: int | None = None, time_to: int | None = None
    ) -> list[dict]:
        """
        Get historical price data (OHLCV candles) for a token.

        Args:
            token_address: The token's mint address
            interval: Candle interval ("1m", "5m", "15m", "1H", "4H", "1D")
            time_from: Start time as Unix timestamp
            time_to: End time as Unix timestamp
        """
        now = int(datetime.now(timezone.utc).timestamp())
        params = {
            "address": token_address,
            "type": interval,
            "time_from": time_from or (now - 30 * 86400),  # Default: 30 days ago
            "time_to": time_to or now,
        }
        data = await self._get("/defi/history_price", params)
        return data.get("data", {}).get("items", [])

    async def get_token_top_traders(self, token_address: str, time_range: str = "30d") -> list[dict]:
        """
        Get the top traders for a specific token.
        This is GOLD for Stage 2 — it shows us who made the most money on each token.

        Args:
            token_address: The token's mint address
            time_range: "24h", "7d", "30d"
        """
        params = {
            "address": token_address,
            "time_frame": time_range,
            "sort_by": "PnL",
            "sort_type": "desc",
        }
        data = await self._get("/trader/gainers-losers", params)
        return data.get("data", {}).get("items", [])


class DexScreenerClient:
    """
    Client for the DexScreener API — our secondary source for token discovery.

    DexScreener is free (no API key needed) and provides:
    - Trending pairs across all DEXs
    - Token profiles with boosted visibility
    - Real-time price and volume data

    Great for catching newly trending tokens that might not show up
    in Birdeye's historical rankings yet.

    API docs: https://docs.dexscreener.com/api/reference
    """

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _get(self, endpoint: str) -> dict | list:
        """Make a GET request to the DexScreener API."""
        url = f"{self.BASE_URL}{endpoint}"
        async with self.session.get(url) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 429:
                logger.warning("dexscreener_rate_limited", endpoint=endpoint)
                await asyncio.sleep(5)
                return await self._get(endpoint)
            else:
                error_text = await response.text()
                logger.error("dexscreener_error", status=response.status, error=error_text)
                return {}

    async def get_trending_tokens(self) -> list[dict]:
        """
        Get currently boosted/trending token profiles on DexScreener.
        These are tokens that are getting visibility (some paid, some organic).
        """
        data = await self._get("/token-boosts/latest/v1")
        return data if isinstance(data, list) else []

    async def get_top_solana_pairs(self) -> list[dict]:
        """
        Get top trading pairs on Solana by volume/activity.
        Returns pairs with full metadata (price, volume, liquidity, etc.)
        """
        data = await self._get("/latest/dex/search?q=solana")
        return data.get("pairs", []) if isinstance(data, dict) else []

    async def search_token(self, query: str) -> list[dict]:
        """
        Search for a specific token by name or symbol.
        Useful for looking up tokens we found from other sources.
        """
        data = await self._get(f"/latest/dex/search?q={query}")
        return data.get("pairs", []) if isinstance(data, dict) else []

    async def get_token_pairs(self, token_address: str) -> list[dict]:
        """
        Get all trading pairs for a specific token.
        A token can trade on multiple DEXs (Raydium, Orca, etc.)
        """
        data = await self._get(f"/tokens/v1/solana/{token_address}")
        return data if isinstance(data, list) else []


class TokenScanner:
    """
    Main token discovery engine.

    Combines data from Birdeye and DexScreener to find the best-performing
    Solana tokens that match our criteria (market cap range, min gain, etc.)

    Usage:
        scanner = TokenScanner(settings, db)
        await scanner.initialize()
        tokens = await scanner.run_discovery()
        await scanner.close()
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.session: aiohttp.ClientSession | None = None
        self.birdeye: BirdeyeClient | None = None
        self.dexscreener: DexScreenerClient | None = None
        self.gmgn: GMGNClient | None = None
        self.gecko: GeckoTerminalClient | None = None

    async def initialize(self) -> None:
        """Set up HTTP session and API clients."""
        self.session = aiohttp.ClientSession()
        self.birdeye = BirdeyeClient(self.settings.birdeye_api_key, self.session)
        self.dexscreener = DexScreenerClient(self.session)
        self.gmgn = GMGNClient(cf_clearance=self.settings.gmgn_cf_clearance, cf_bm=self.settings.gmgn_cf_bm)
        self.gecko = GeckoTerminalClient(self.session)
        logger.info("token_scanner_initialized")

    async def close(self) -> None:
        """Clean up HTTP sessions."""
        if self.session:
            await self.session.close()
        if self.gmgn:
            self.gmgn.close()

    async def run_discovery(self) -> list[dict]:
        """
        Run the full token discovery pipeline.
        Returns a list of qualifying tokens sorted by performance.

        v2: Uses GMGN as primary source (pre-ranked tokens with safety data),
        DexScreener as secondary. Birdeye only used for fallback enrichment.
        """
        logger.info(
            "discovery_starting",
            lookback_days=self.settings.discovery_lookback_days,
            min_multiplier=f"{self.settings.min_price_multiplier}x",
            market_cap_range=f"${self.settings.min_market_cap_usd/1e6:.0f}M-${self.settings.max_market_cap_usd/1e6:.0f}M",
        )

        # Step 1: Gather token candidates from multiple sources
        all_candidates = []

        # Source 1 (PRIMARY): GeckoTerminal — trending + top volume pools
        # Free, no auth, no Cloudflare. Returns pools with full price data.
        gecko_tokens = await self._fetch_geckoterminal_tokens()
        all_candidates.extend(gecko_tokens)
        logger.info("geckoterminal_tokens_fetched", count=len(gecko_tokens))

        # Source 2 (SECONDARY): DexScreener trending tokens
        dex_tokens = await self._fetch_dexscreener_tokens()
        all_candidates.extend(dex_tokens)
        logger.info("dexscreener_tokens_fetched", count=len(dex_tokens))

        # Source 3 (OPTIONAL): GMGN — only if we have cookie auth configured
        # GMGN is behind Cloudflare, so this will silently fail without cookies
        gmgn_tokens = await self._fetch_gmgn_tokens()
        if gmgn_tokens:
            all_candidates.extend(gmgn_tokens)
            logger.info("gmgn_tokens_fetched", count=len(gmgn_tokens))

        # Step 2: Deduplicate by mint address
        unique_tokens = self._deduplicate(all_candidates)
        logger.info("unique_tokens_after_dedup", count=len(unique_tokens))

        # Step 3: Filter by our criteria (GMGN tokens already have multipliers)
        qualifying = self._filter_tokens(unique_tokens)
        logger.info("qualifying_tokens", count=len(qualifying))

        # Step 4: Sort by performance (best first)
        qualifying.sort(key=lambda t: t.get("price_multiplier") or 0, reverse=True)

        # Step 5: Save to database
        saved_count = 0
        for token in qualifying:
            await self.db.insert_token(token)
            saved_count += 1

        logger.info("discovery_complete", tokens_found=len(qualifying), saved=saved_count)

        # Print a summary table
        self._print_summary(qualifying[:20])

        return qualifying

    async def _fetch_birdeye_tokens(self) -> list[dict]:
        """
        Fetch tokens from Birdeye, paginating through results.
        We pull tokens sorted by volume (active tokens are more interesting)
        and by market cap within our range.
        """
        tokens = []

        # Fetch tokens sorted by 24h volume (most active first)
        for offset in range(0, self.settings.max_discovery_tokens, 50):
            batch = await self.birdeye.get_token_list(
                sort_by="v24hUSD",
                sort_type="desc",
                min_market_cap=self.settings.min_market_cap_usd,
                max_market_cap=self.settings.max_market_cap_usd,
                limit=50,
                offset=offset,
            )
            if not batch:
                break

            for t in batch:
                tokens.append(self._normalize_birdeye_token(t))

            # Small delay to respect rate limits
            await asyncio.sleep(0.5)

        return tokens

    async def _fetch_geckoterminal_tokens(self) -> list[dict]:
        """
        Fetch tokens from GeckoTerminal (PRIMARY source).

        Pulls from two endpoints:
        1. Trending pools — currently hot tokens
        2. Top volume pools — actively traded tokens

        GeckoTerminal returns pool data, so we normalize to our token format.
        The h24 price change is converted to a multiplier.
        """
        tokens = []
        seen_mints = set()

        # Get trending pools (3 pages = ~60 pools)
        trending = await self.gecko.get_trending_pools(pages=3)
        for pool in trending:
            normalized = self._normalize_gecko_pool(pool)
            if normalized and normalized["mint_address"] not in seen_mints:
                seen_mints.add(normalized["mint_address"])
                tokens.append(normalized)

        # Get top volume pools (3 pages = ~60 pools)
        top_volume = await self.gecko.get_top_pools_by_volume(pages=3)
        for pool in top_volume:
            normalized = self._normalize_gecko_pool(pool)
            if normalized and normalized["mint_address"] not in seen_mints:
                seen_mints.add(normalized["mint_address"])
                tokens.append(normalized)

        return tokens

    def _normalize_gecko_pool(self, pool: dict) -> dict | None:
        """
        Convert GeckoTerminal pool data to our standard token format.

        The price_change_h24 field gives us the 24h price change as a percentage.
        We also use the best of h6 and h24 to calculate the multiplier,
        since some tokens may have pumped within a shorter window.
        """
        token_address = pool.get("token_address", "")
        symbol = pool.get("token_symbol", "")
        if not token_address or not symbol:
            return None

        # Skip wrapped SOL, USDC, USDT and other stables
        if symbol.upper() in {"SOL", "WSOL", "USDC", "USDT", "USDS", "DAI", "BUSD"}:
            return None

        market_cap = pool.get("market_cap_usd") or pool.get("fdv_usd") or 0
        liquidity = pool.get("reserve_usd") or 0
        volume = pool.get("volume_h24") or 0
        price = pool.get("price_usd") or 0

        # Use the best price change to calculate multiplier
        # A token might have pumped in 6h and leveled off in 24h
        h24_change = pool.get("price_change_h24") or 0
        h6_change = pool.get("price_change_h6") or 0
        best_change = max(h24_change, h6_change)

        # Convert percentage to multiplier (e.g., 500% = 6x)
        multiplier = 1 + (best_change / 100) if best_change > 0 else 0

        return {
            "mint_address": token_address,
            "symbol": symbol,
            "name": pool.get("token_name") or symbol,
            "market_cap_usd": market_cap,
            "price_usd": price,
            "price_change_pct": h24_change,
            "price_multiplier": multiplier if multiplier > 1 else None,
            "volume_24h_usd": volume,
            "liquidity_usd": liquidity,
            "holder_count": pool.get("buyers_h24") or 0,  # Proxy: unique buyers in 24h
            "pair_address": pool.get("pool_address"),
            "dex_name": pool.get("dex_id"),
            "data_source": "geckoterminal",
        }

    async def _fetch_gmgn_tokens(self) -> list[dict]:
        """
        Fetch top-performing tokens from GMGN.ai (PRIMARY source).

        GMGN returns up to 100 tokens per timeframe, pre-sorted by price change.
        Each token comes with safety metrics (rug_ratio, wash trading, bundler rate)
        so we don't need separate enrichment calls.

        We pull from multiple timeframes to catch both recent runners and sustained performers.
        """
        tokens = []
        seen_mints = set()

        # Pull from multiple timeframes to cast a wider net
        for timeframe in ["24h", "6h", "1h"]:
            raw_tokens = await self.gmgn.get_top_tokens(
                timeframe=timeframe,
                order_by="price_change_percent",
                direction="desc",
                min_liquidity=int(self.settings.min_liquidity_usd),
            )

            for raw in raw_tokens:
                normalized = self._normalize_gmgn_token(raw)
                if normalized and normalized["mint_address"] not in seen_mints:
                    seen_mints.add(normalized["mint_address"])
                    tokens.append(normalized)

            # Small delay between timeframe calls
            await asyncio.sleep(0.5)

        return tokens

    def _normalize_gmgn_token(self, raw: dict) -> dict | None:
        """
        Convert GMGN's token format to our standard format.

        GMGN fields we care about:
        - address: token mint address
        - symbol, name: token identity
        - market_cap, price, volume: market data
        - liquidity: pool liquidity
        - holder_count: number of holders
        - price_change_percent: percentage change in the timeframe
        - rug_ratio: 0-1, higher = more rug-like
        - is_wash_trading: boolean
        - renounced_mint: whether mint authority is renounced
        - smart_degen_count: how many known smart wallets are trading it
        - renowned_count: how many "renowned" wallets hold it
        """
        address = raw.get("address") or raw.get("mint")
        if not address:
            return None

        # Extract price change and convert to multiplier
        price_change_pct = raw.get("price_change_percent") or raw.get("price_change_percent1h") or 0
        if isinstance(price_change_pct, str):
            try:
                price_change_pct = float(price_change_pct)
            except (ValueError, TypeError):
                price_change_pct = 0

        # Convert percentage to multiplier (e.g., 500% change = 6x)
        multiplier = 1 + (price_change_pct / 100) if price_change_pct else 0

        # Market data — GMGN uses various key names
        market_cap = raw.get("market_cap") or raw.get("marketcap") or raw.get("usd_market_cap") or 0
        liquidity = raw.get("liquidity") or 0
        volume = raw.get("volume") or raw.get("swaps_amount") or 0
        price = raw.get("price") or 0
        holders = raw.get("holder_count") or 0

        # Ensure numeric types
        try:
            market_cap = float(market_cap) if market_cap else 0
            liquidity = float(liquidity) if liquidity else 0
            volume = float(volume) if volume else 0
            price = float(price) if price else 0
            holders = int(holders) if holders else 0
        except (ValueError, TypeError):
            return None

        return {
            "mint_address": address,
            "symbol": raw.get("symbol") or "UNKNOWN",
            "name": raw.get("name") or raw.get("symbol") or "",
            "market_cap_usd": market_cap,
            "price_usd": price,
            "price_change_pct": price_change_pct,
            "price_multiplier": multiplier if multiplier > 0 else None,
            "volume_24h_usd": volume,
            "liquidity_usd": liquidity,
            "holder_count": holders,
            "pair_address": None,
            "dex_name": None,
            "data_source": "gmgn",
            # GMGN safety fields — stored for filtering
            "gmgn_rug_ratio": raw.get("rug_ratio"),
            "gmgn_is_wash_trading": raw.get("is_wash_trading"),
            "gmgn_bundler_rate": raw.get("bundler_rate"),
            "gmgn_renounced_mint": raw.get("renounced_mint"),
            "gmgn_smart_degen_count": raw.get("smart_degen_count") or 0,
            "gmgn_renowned_count": raw.get("renowned_count") or 0,
        }

    async def _fetch_dexscreener_tokens(self) -> list[dict]:
        """
        Fetch trending/boosted tokens from DexScreener.
        Filter to Solana tokens within our market cap range.
        """
        tokens = []

        # Get trending tokens
        trending = await self.dexscreener.get_trending_tokens()
        for t in trending:
            # Only Solana tokens
            if t.get("chainId") != "solana":
                continue
            # Get full pair data for this token
            pairs = await self.dexscreener.get_token_pairs(t.get("tokenAddress", ""))
            if pairs:
                # Use the pair with the highest liquidity
                best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                normalized = self._normalize_dexscreener_pair(best_pair)
                if normalized:
                    tokens.append(normalized)

            await asyncio.sleep(0.3)  # Rate limiting

        return tokens

    def _normalize_birdeye_token(self, raw: dict) -> dict:
        """
        Convert Birdeye's token format to our standard format.
        Different APIs return data in different formats, so we normalize
        everything to a single format before processing.
        """
        return {
            "mint_address": raw.get("address", ""),
            "symbol": raw.get("symbol", "UNKNOWN"),
            "name": raw.get("name", ""),
            "market_cap_usd": raw.get("mc") or raw.get("market_cap") or 0,
            "price_usd": raw.get("price") or raw.get("lastTradeUnixTime") or 0,
            "price_change_pct": raw.get("priceChange24hPercent") or raw.get("price_change_24h_percent") or 0,
            "price_multiplier": None,  # Will be calculated during enrichment
            "volume_24h_usd": raw.get("v24hUSD") or raw.get("volume_24h") or 0,
            "liquidity_usd": raw.get("liquidity") or 0,
            "holder_count": raw.get("holder") or 0,
            "pair_address": None,
            "dex_name": None,
            "data_source": "birdeye",
        }

    def _normalize_dexscreener_pair(self, pair: dict) -> dict | None:
        """
        Convert DexScreener's pair format to our standard format.
        Returns None if the pair doesn't have enough data.
        """
        if not pair.get("baseToken", {}).get("address"):
            return None

        market_cap = float(pair.get("marketCap") or pair.get("fdv") or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)

        # Calculate price multiplier from price change data
        price_change = pair.get("priceChange", {})
        # DexScreener gives percentage changes, not multipliers
        pct_change_24h = float(price_change.get("h24", 0) or 0)

        return {
            "mint_address": pair["baseToken"]["address"],
            "symbol": pair["baseToken"].get("symbol", "UNKNOWN"),
            "name": pair["baseToken"].get("name", ""),
            "market_cap_usd": market_cap,
            "price_usd": float(pair.get("priceUsd", 0) or 0),
            "price_change_pct": pct_change_24h,
            "price_multiplier": None,  # Will be calculated during enrichment
            "volume_24h_usd": float(pair.get("volume", {}).get("h24", 0) or 0),
            "liquidity_usd": liquidity,
            "holder_count": 0,  # DexScreener doesn't always provide this
            "pair_address": pair.get("pairAddress"),
            "dex_name": pair.get("dexId"),
            "data_source": "dexscreener",
        }

    def _deduplicate(self, tokens: list[dict]) -> list[dict]:
        """
        Remove duplicate tokens (same mint address from different sources).
        When there's a duplicate, keep the one with more complete data.
        """
        seen = {}
        for token in tokens:
            mint = token.get("mint_address")
            if not mint:
                continue
            if mint not in seen:
                seen[mint] = token
            else:
                # Keep the one with more data (higher market cap or more fields filled)
                existing = seen[mint]
                if (token.get("market_cap_usd") or 0) > (existing.get("market_cap_usd") or 0):
                    seen[mint] = token
        return list(seen.values())

    async def _enrich_tokens(self, tokens: list[dict]) -> list[dict]:
        """
        Add missing data to tokens using Birdeye's detailed token overview.
        Most importantly, calculate the price_multiplier (how many X the token did).
        """
        enriched = []
        for token in tokens:
            mint = token["mint_address"]

            # If we don't have a price multiplier, try to calculate it
            if not token.get("price_multiplier"):
                try:
                    # Get price history to calculate the actual multiplier
                    history = await self.birdeye.get_token_price_history(
                        mint,
                        interval="1D",
                    )
                    if history and len(history) >= 2:
                        # Find the lowest price in our lookback period
                        prices = [candle.get("value", 0) for candle in history if candle.get("value", 0) > 0]
                        if prices:
                            min_price = min(prices)
                            current_price = token.get("price_usd") or prices[-1]
                            if min_price > 0:
                                token["price_multiplier"] = current_price / min_price

                    await asyncio.sleep(0.3)  # Rate limiting
                except Exception as e:
                    logger.debug("enrichment_error", token=token.get("symbol"), error=str(e))

            # If we still don't have enough data, try getting the overview
            if not token.get("market_cap_usd") or not token.get("liquidity_usd"):
                try:
                    overview = await self.birdeye.get_token_overview(mint)
                    if overview:
                        token["market_cap_usd"] = token.get("market_cap_usd") or overview.get("mc", 0)
                        token["liquidity_usd"] = token.get("liquidity_usd") or overview.get("liquidity", 0)
                        token["holder_count"] = token.get("holder_count") or overview.get("holder", 0)
                        token["price_usd"] = token.get("price_usd") or overview.get("price", 0)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.debug("overview_error", token=token.get("symbol"), error=str(e))

            enriched.append(token)

        return enriched

    def _filter_tokens(self, tokens: list[dict]) -> list[dict]:
        """
        Filter tokens by strict criteria to eliminate rugs and scams:
        - Market cap between min and max
        - Price multiplier >= minimum (MUST have data — no N/A allowed)
        - Real liquidity (minimum $25K)
        - Healthy liquidity-to-mcap ratio (catches fake mcap rugs)
        - Minimum holder count
        - Clean symbol/name (no random strings)
        - GMGN safety checks: rug_ratio, wash trading, bundler rate
        - Not on blacklist
        """
        import re
        qualifying = []
        for token in tokens:
            mint = token.get("mint_address", "")
            symbol = token.get("symbol", "")
            name = token.get("name", "")
            mcap = token.get("market_cap_usd") or 0
            multiplier = token.get("price_multiplier") or 0
            liquidity = token.get("liquidity_usd") or 0
            holders = token.get("holder_count") or 0
            volume = token.get("volume_24h_usd") or 0

            # Skip blacklisted tokens
            if mint in self.settings.token_blacklist:
                logger.debug("token_filtered", symbol=symbol, reason="blacklisted")
                continue

            # Market cap filter
            if mcap < self.settings.min_market_cap_usd or mcap > self.settings.max_market_cap_usd:
                continue

            # MUST have price multiplier data — no N/A tokens
            if not multiplier or multiplier <= 0:
                logger.debug("token_filtered", symbol=symbol, reason="no_multiplier_data")
                continue

            # Minimum performance filter
            if multiplier < self.settings.min_price_multiplier:
                continue

            # Minimum liquidity — $25K floor to ensure we can actually sell
            if liquidity < 25_000:
                logger.debug("token_filtered", symbol=symbol, reason=f"low_liquidity_${liquidity:.0f}")
                continue

            # Liquidity-to-mcap ratio — rugs have huge mcap but tiny liquidity
            if mcap > 0 and (liquidity / mcap) < 0.005:
                logger.debug("token_filtered", symbol=symbol, reason=f"bad_liq_ratio_{liquidity/mcap:.4f}")
                continue

            # Minimum 24h volume — dead tokens aren't interesting
            if volume < 10_000:
                logger.debug("token_filtered", symbol=symbol, reason=f"low_volume_${volume:.0f}")
                continue

            # === GMGN Safety Checks (when available) ===
            # These fields only exist for tokens from GMGN source
            rug_ratio = token.get("gmgn_rug_ratio")
            if rug_ratio is not None:
                try:
                    if float(rug_ratio) > 0.5:
                        logger.debug("token_filtered", symbol=symbol, reason=f"high_rug_ratio_{rug_ratio}")
                        continue
                except (ValueError, TypeError):
                    pass

            if token.get("gmgn_is_wash_trading"):
                logger.debug("token_filtered", symbol=symbol, reason="wash_trading_detected")
                continue

            bundler_rate = token.get("gmgn_bundler_rate")
            if bundler_rate is not None:
                try:
                    if float(bundler_rate) > 0.3:
                        logger.debug("token_filtered", symbol=symbol, reason=f"high_bundler_rate_{bundler_rate}")
                        continue
                except (ValueError, TypeError):
                    pass

            # Symbol sanity check — filter out random gibberish names
            if not re.match(r'^[A-Za-z0-9$. ]{1,15}$', symbol):
                logger.debug("token_filtered", symbol=symbol, reason="suspicious_symbol")
                continue

            # Filter tokens with spaces in symbol (often scams like "BVB XMN5")
            if ' ' in symbol.strip():
                logger.debug("token_filtered", symbol=symbol, reason="multi_word_symbol")
                continue

            # Filter random-looking symbols (3+ consecutive consonants or digits)
            if re.match(r'^[a-z0-9]+$', symbol.lower()) and len(symbol) > 4:
                consonant_runs = re.findall(r'[bcdfghjklmnpqrstvwxyz]{3,}', symbol.lower())
                digit_runs = re.findall(r'[0-9]{2,}', symbol)
                if consonant_runs or digit_runs:
                    logger.debug("token_filtered", symbol=symbol, reason="random_looking_symbol")
                    continue

            qualifying.append(token)

        return qualifying

    def _print_summary(self, tokens: list[dict]) -> None:
        """Print a nice summary table of discovered tokens."""
        if not tokens:
            logger.info("no_qualifying_tokens_found")
            return

        logger.info("=" * 80)
        logger.info("TOP PERFORMING TOKENS DISCOVERED")
        logger.info("=" * 80)

        for i, token in enumerate(tokens, 1):
            multiplier = token.get("price_multiplier") or 0
            mcap = token.get("market_cap_usd") or 0
            volume = token.get("volume_24h_usd") or 0
            liquidity = token.get("liquidity_usd") or 0

            logger.info(
                f"#{i}",
                symbol=token.get("symbol", "???"),
                multiplier=f"{multiplier:.1f}x" if multiplier else "N/A",
                market_cap=f"${mcap/1e6:.1f}M",
                volume_24h=f"${volume/1e6:.1f}M",
                liquidity=f"${liquidity/1e3:.0f}K",
                source=token.get("data_source"),
            )

        logger.info("=" * 80)
