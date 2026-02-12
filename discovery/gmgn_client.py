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

    async def get_smart_money_wallets(
        self,
        min_profit_30d: float = 1000.0,
        min_winrate: float = 0.4,
        min_buys_30d: int = 5,
        max_buys_30d: int = 15000,
        min_sol_balance: float = 0.5,
    ) -> list[dict]:
        """
        Find smart money wallets by scanning recent top tokens and filtering
        their buyers through strict quality gates.

        Strategy:
        1. Try GMGN's smart money leaderboard endpoint (undocumented, may fail)
        2. If that fails, use top tokens → top buyers → enrich → filter

        Returns list of enriched wallet dicts that pass all filters.
        """
        BAD_TAGS = {"sandwich_bot", "scammer", "rug_deployer", "sniper_bot", "mev_bot", "copy_bot", "arb_bot"}

        # --- Attempt 1: Try the leaderboard endpoint directly ---
        logger.info("smart_money_trying_leaderboard")
        leaderboard_data = await self._get(
            "/smartmoney/sol/walletNew",
            params={"limit": "100", "orderby": "realized_profit_30d", "direction": "desc"},
        )

        leaderboard_wallets = []
        if leaderboard_data:
            # Try different response structures
            raw = leaderboard_data.get("data", {})
            if isinstance(raw, list):
                leaderboard_wallets = raw
            elif isinstance(raw, dict):
                leaderboard_wallets = raw.get("rank", []) or raw.get("wallets", []) or raw.get("list", [])

        if leaderboard_wallets:
            logger.info("smart_money_leaderboard_hit", count=len(leaderboard_wallets))
            # Leaderboard wallets likely already have stats — extract addresses and enrich
            addresses = []
            for w in leaderboard_wallets:
                addr = w.get("wallet_address") or w.get("address") or w.get("walletAddress")
                if addr:
                    addresses.append(addr)
            if addresses:
                return await self._enrich_and_filter(
                    addresses[:100], min_profit_30d, min_winrate,
                    min_buys_30d, max_buys_30d, min_sol_balance, BAD_TAGS,
                )

        # --- Attempt 2: Token-buyers approach ---
        logger.info("smart_money_using_token_buyers_approach")

        # Get recent top-moving tokens across multiple timeframes
        all_tokens = []
        for tf in ["1h", "6h", "24h"]:
            tokens = await self.get_top_tokens(tf, min_marketcap=100_000, min_liquidity=10_000)
            all_tokens.extend(tokens)
            await asyncio.sleep(0.5)

        if not all_tokens:
            logger.warning("smart_money_no_tokens_found")
            return []

        logger.info("smart_money_tokens_fetched", count=len(all_tokens))

        # Deduplicate tokens by address
        seen_tokens = set()
        unique_tokens = []
        for t in all_tokens:
            addr = t.get("address") or t.get("mint") or t.get("token_address")
            if addr and addr not in seen_tokens:
                seen_tokens.add(addr)
                unique_tokens.append(t)

        # Cap at 30 tokens to keep API calls reasonable
        unique_tokens = unique_tokens[:30]

        # Get top buyers for each token
        all_buyer_addresses = set()
        for t in unique_tokens:
            token_addr = t.get("address") or t.get("mint") or t.get("token_address")
            if not token_addr:
                continue
            buyers = await self.get_top_buyers(token_addr)
            for b in buyers:
                addr = b.get("wallet_address") or b.get("address")
                if addr:
                    all_buyer_addresses.add(addr)
            await asyncio.sleep(0.3)

        logger.info("smart_money_unique_buyers", count=len(all_buyer_addresses))

        if not all_buyer_addresses:
            return []

        # Cap at 200 wallets to enrich (each costs 1 API call)
        buyer_list = list(all_buyer_addresses)[:200]

        return await self._enrich_and_filter(
            buyer_list, min_profit_30d, min_winrate,
            min_buys_30d, max_buys_30d, min_sol_balance, BAD_TAGS,
        )

    async def _enrich_and_filter(
        self,
        addresses: list[str],
        min_profit_30d: float,
        min_winrate: float,
        min_buys_30d: int,
        max_buys_30d: int,
        min_sol_balance: float,
        bad_tags: set,
    ) -> list[dict]:
        """Enrich wallet addresses with stats and apply smart money filters."""

        def _float(val, default=0.0):
            """Safely convert GMGN value to float (handles strings, None)."""
            if val is None:
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        passed = []
        total_checked = 0

        for i, addr in enumerate(addresses):
            stats = await self.get_wallet_stats(addr)
            if not stats:
                await asyncio.sleep(0.3)
                continue

            total_checked += 1

            # Extract and safely convert all fields
            profit_30d = _float(stats.get("realized_profit_30d"))
            winrate = stats.get("winrate")  # Often None
            buy_30d = int(_float(stats.get("buy_30d")))
            sell_30d = int(_float(stats.get("sell_30d")))
            sol_balance = _float(stats.get("sol_balance"))
            tags = stats.get("tags") or []
            if isinstance(tags, str):
                try:
                    import json
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = [tags] if tags else []

            realized_profit = _float(stats.get("realized_profit"))

            # --- Apply filters ---

            # Filter 1: Must have profit data
            if profit_30d <= 0 and realized_profit <= 0:
                continue

            # Filter 2: Minimum 30D profit
            if profit_30d < min_profit_30d:
                continue

            # Filter 3: Winrate — REQUIRE it. NULL winrate = unverified = reject.
            # Previously NULL winrate passed, letting unvetted snipers through.
            if winrate is None or _float(winrate) < min_winrate:
                continue

            # Filter 4: Activity range (not inactive, not a bot)
            if buy_30d < min_buys_30d or buy_30d > max_buys_30d:
                continue

            # Filter 5: Has SOL to trade with
            if sol_balance < min_sol_balance:
                continue

            # Filter 6: No bad tags (bot platforms + known bad actors)
            tag_set = set(t.lower().replace(" ", "_") for t in tags) if tags else set()
            if tag_set & bad_tags:
                continue

            # Filter 7: No sniper bot platforms (axiom, photon, bullx)
            # These are profitable but uncopyable — they buy tokens within
            # milliseconds of launch, way too fast for copy trading.
            SNIPER_PLATFORMS = {"axiom", "photon", "bullx"}
            if tag_set & SNIPER_PLATFORMS:
                continue

            # Passed all filters — build wallet dict
            passed.append({
                "address": addr,
                "gmgn_realized_profit_usd": realized_profit,
                "gmgn_profit_30d_usd": profit_30d,
                "gmgn_sol_balance": sol_balance,
                "gmgn_winrate": _float(winrate) if winrate is not None else None,
                "gmgn_buy_30d": buy_30d,
                "gmgn_sell_30d": sell_30d,
                "gmgn_tags": tags,
            })

            # Log progress every 50 wallets
            if total_checked % 50 == 0:
                logger.info("smart_money_progress", checked=total_checked, passed=len(passed))

            await asyncio.sleep(0.5)  # Rate limiting

        logger.info(
            "smart_money_scan_complete",
            total_checked=total_checked,
            passed_filters=len(passed),
        )

        # Sort by 30D profit descending
        passed.sort(key=lambda w: w.get("gmgn_profit_30d_usd", 0), reverse=True)
        return passed

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
