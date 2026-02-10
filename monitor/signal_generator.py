"""
Signal Generator
================
Adds validation and intelligence on top of raw wallet buy signals.

When the WalletMonitor detects a smart wallet buying a token,
the signal comes here BEFORE going to the trade executor.

This module checks:
1. Is the token safe to buy? (liquidity, honeypot, blacklist)
2. Do we already have a position in this token?
3. Are we within our risk limits? (max positions, daily loss)
4. Are multiple smart wallets buying the same token? (stronger signal)
5. Does the market cap meet our trading threshold?

Think of this as the "risk manager" that sits between detection and execution.
The monitor says "smart wallet bought something!" and this module decides
if we should follow.
"""

import asyncio
from datetime import datetime, timezone

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.logger import get_logger

logger = get_logger(__name__)


class SignalGenerator:
    """
    Validates and enriches raw buy signals before sending to trade executor.

    Hybrid strategy: position sizing varies by wallet type.
    - Human wallets (<50 trades/day): full position size
    - Bot wallets (50+ trades/day): half position size (faster but riskier)
    - Consensus (2+ wallets buy same token in 5 min): double position size

    Usage:
        sig_gen = SignalGenerator(settings, db)
        await sig_gen.initialize()
        should_trade, enriched = await sig_gen.validate_signal(raw_signal)
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.session: aiohttp.ClientSession | None = None
        # Track recent buy signals for consensus detection
        # {token_mint: [(wallet_address, timestamp), ...]}
        self._recent_buys: dict[str, list[tuple[str, float]]] = {}

    async def initialize(self) -> None:
        """Set up HTTP session for API calls."""
        self.session = aiohttp.ClientSession()
        logger.info("signal_generator_initialized")

    async def close(self) -> None:
        """Clean up."""
        if self.session:
            await self.session.close()

    async def validate_signal(self, signal: dict) -> tuple[bool, dict, str]:
        """
        Validate a raw buy signal and decide if we should copy it.

        Returns:
            (should_trade, enriched_signal, skip_reason)
            - should_trade: True if we should execute the copy trade
            - enriched_signal: Signal with added token data
            - skip_reason: Why we're skipping (empty string if we should trade)
        """
        token_mint = signal["token_mint"]

        # Check 1: Kill switch
        if self.settings.trading_paused:
            return False, signal, "Trading is paused (kill switch active)"

        # Check 2: Blacklist
        if token_mint in self.settings.token_blacklist:
            await self.db.mark_signal_skipped(signal.get("signal_id", 0), "blacklisted")
            return False, signal, f"Token is blacklisted"

        # Check 3: Max open positions
        open_count = await self.db.get_open_position_count()
        if open_count >= self.settings.max_open_positions:
            await self.db.mark_signal_skipped(signal.get("signal_id", 0), "max_positions_reached")
            return False, signal, f"Max positions reached ({open_count}/{self.settings.max_open_positions})"

        # Check 4: Daily loss limit
        daily_pnl = await self.db.get_todays_pnl()
        if daily_pnl <= -self.settings.max_daily_loss_sol:
            self.settings.trading_paused = True  # Auto-pause
            await self.db.mark_signal_skipped(signal.get("signal_id", 0), "daily_loss_limit")
            return False, signal, f"Daily loss limit hit ({daily_pnl:.4f} SOL)"

        # Check 5: Already have a position in this token?
        existing_position = await self.db.get_position_by_token(token_mint)
        if existing_position:
            # Check if we've hit our max position size for this token
            invested = existing_position.get("amount_sol_invested", 0)
            if invested >= self.settings.max_position_size_sol:
                await self.db.mark_signal_skipped(signal.get("signal_id", 0), "max_position_size")
                return False, signal, f"Max position size reached for this token ({invested:.4f} SOL)"

        # Check 6: Get token data (price, liquidity, market cap)
        # Try Birdeye first, fall back to DexScreener
        token_data = await self._get_token_data(token_mint)
        if not token_data:
            token_data = await self._get_token_data_dexscreener(token_mint)
        signal["token_data"] = token_data

        if token_data:
            symbol = token_data.get("symbol", "???")
            signal["token_symbol"] = symbol

            # Check liquidity
            liquidity = token_data.get("liquidity_usd", 0)
            if liquidity < self.settings.min_liquidity_usd:
                reason = f"Insufficient liquidity: ${liquidity:,.0f} (min: ${self.settings.min_liquidity_usd:,.0f})"
                await self.db.mark_signal_skipped(signal.get("signal_id", 0), "low_liquidity")
                return False, signal, reason

            # Check market cap against COPY TRADING range (not discovery range)
            mcap = token_data.get("market_cap_usd", 0)
            if mcap > 0:
                if mcap < self.settings.min_copy_trade_mcap_usd:
                    reason = f"Market cap too low for copy trade: ${mcap:,.0f}"
                    await self.db.mark_signal_skipped(signal.get("signal_id", 0), "mcap_too_low")
                    return False, signal, reason
                if mcap > self.settings.max_copy_trade_mcap_usd:
                    reason = f"Market cap too high: ${mcap:,.0f}"
                    await self.db.mark_signal_skipped(signal.get("signal_id", 0), "mcap_too_high")
                    return False, signal, reason

        # Check 7: Honeypot detection (can we actually sell this token?)
        is_honeypot = await self._check_honeypot(token_mint)
        if is_honeypot:
            await self.db.mark_signal_skipped(signal.get("signal_id", 0), "honeypot_detected")
            return False, signal, "Honeypot detected — token cannot be sold"

        # All checks passed! Now determine wallet type and position size.

        # Detect wallet type: human vs bot
        wallet_type = await self._get_wallet_type(signal["wallet_address"])
        signal["signal_source_type"] = wallet_type

        # Check for consensus (2+ wallets buying same token within window)
        now_ts = datetime.now(timezone.utc).timestamp()
        self._record_buy(token_mint, signal["wallet_address"], now_ts)
        consensus_count = self._check_consensus(token_mint, now_ts)

        if consensus_count >= 2:
            signal["signal_source_type"] = "consensus"
            signal["consensus_count"] = consensus_count

        # Set position size based on wallet type
        base_size = self.settings.default_position_size_sol
        source_type = signal["signal_source_type"]

        if source_type == "consensus":
            position_size = base_size * self.settings.consensus_position_multiplier
        elif source_type == "bot":
            position_size = base_size * self.settings.bot_position_multiplier
        else:
            position_size = base_size

        signal["position_size_sol"] = position_size

        logger.info(
            "signal_validated",
            token=signal.get("token_symbol", token_mint[:8]),
            wallet=signal["wallet_address"][:8] + "...",
            wallet_type=source_type,
            position_size=f"{position_size:.4f} SOL",
            consensus=consensus_count if consensus_count >= 2 else None,
            liquidity=f"${token_data.get('liquidity_usd', 0):,.0f}" if token_data else "unknown",
            confidence=f"{signal.get('confidence', 0):.2f}",
        )

        return True, signal, ""

    async def _get_token_data(self, token_mint: str) -> dict | None:
        """
        Fetch current token data from Birdeye.
        Returns price, liquidity, market cap, etc.
        """
        try:
            url = "https://public-api.birdeye.so/defi/token_overview"
            headers = {
                "X-API-KEY": self.settings.birdeye_api_key,
                "x-chain": "solana",
            }
            params = {"address": token_mint}

            async with self.session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    token_info = data.get("data", {})
                    return {
                        "symbol": token_info.get("symbol", "???"),
                        "name": token_info.get("name", ""),
                        "price_usd": token_info.get("price", 0),
                        "market_cap_usd": token_info.get("mc", 0),
                        "liquidity_usd": token_info.get("liquidity", 0),
                        "volume_24h_usd": token_info.get("v24hUSD", 0),
                        "holder_count": token_info.get("holder", 0),
                    }
                else:
                    logger.warning("token_data_fetch_failed", status=response.status)
                    return None

        except Exception as e:
            logger.error("token_data_error", error=str(e))
            return None

    async def _get_token_data_dexscreener(self, token_mint: str) -> dict | None:
        """
        Fallback: Fetch token data from DexScreener (free, no API key).
        Used when Birdeye fails (new tokens, rate limits, etc.)
        """
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
            async with self.session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    pairs = data.get("pairs") or []
                    if pairs:
                        # Use highest-liquidity pair
                        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
                        return {
                            "symbol": best.get("baseToken", {}).get("symbol", "???"),
                            "name": best.get("baseToken", {}).get("name", ""),
                            "price_usd": float(best.get("priceUsd") or 0),
                            "market_cap_usd": float(best.get("marketCap") or 0),
                            "liquidity_usd": float((best.get("liquidity") or {}).get("usd", 0) or 0),
                            "volume_24h_usd": float((best.get("volume") or {}).get("h24", 0) or 0),
                            "holder_count": 0,  # DexScreener doesn't provide this
                        }
        except Exception as e:
            logger.debug("dexscreener_fallback_error", error=str(e))
        return None

    # GMGN tags that indicate automated/bot wallets
    BOT_TAGS = {"sandwich_bot", "sniper_bot", "mev_bot", "copy_bot", "arb_bot"}

    async def _get_wallet_type(self, wallet_address: str) -> str:
        """
        Determine if a wallet is human or bot.

        Detection priority:
        1. is_bot_speed flag in DB (already computed by wallet refresher)
        2. GMGN tags — sandwich_bot, sniper_bot, mev_bot etc. are real bots
        3. Trade frequency — 200+/day is clearly automated (real degens do 50-150)

        Returns 'human' or 'bot'.
        """
        try:
            sql = "SELECT is_bot_speed, gmgn_buy_30d, gmgn_sell_30d, gmgn_tags FROM wallets WHERE address = ?"
            cursor = await self.db.connection.execute(sql, (wallet_address,))
            row = await cursor.fetchone()
            if row:
                # Check 1: Pre-computed flag (set by wallet refresher)
                if row["is_bot_speed"]:
                    return "bot"

                # Check 2: GMGN tags
                import json
                tags_raw = row["gmgn_tags"] or "[]"
                try:
                    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or [])
                except (json.JSONDecodeError, TypeError):
                    tags = []
                if set(tags) & self.BOT_TAGS:
                    return "bot"

                # Check 3: Trade frequency (200+/day = clearly automated)
                buy_30d = row["gmgn_buy_30d"] or 0
                sell_30d = row["gmgn_sell_30d"] or 0
                trades_per_day = (buy_30d + sell_30d) / 30
                if trades_per_day >= self.settings.bot_speed_threshold:
                    return "bot"
        except Exception:
            pass
        return "human"

    def _record_buy(self, token_mint: str, wallet_address: str, timestamp: float) -> None:
        """Record a buy signal for consensus detection."""
        if token_mint not in self._recent_buys:
            self._recent_buys[token_mint] = []
        self._recent_buys[token_mint].append((wallet_address, timestamp))

        # Clean up old entries beyond the consensus window
        window = self.settings.consensus_window_seconds
        self._recent_buys[token_mint] = [
            (w, t) for w, t in self._recent_buys[token_mint]
            if timestamp - t <= window
        ]

    def _check_consensus(self, token_mint: str, now_ts: float) -> int:
        """
        Count how many unique wallets bought this token within the consensus window.
        Returns the count of unique wallets.
        """
        if token_mint not in self._recent_buys:
            return 0

        window = self.settings.consensus_window_seconds
        recent = self._recent_buys[token_mint]
        unique_wallets = set()
        for wallet, ts in recent:
            if now_ts - ts <= window:
                unique_wallets.add(wallet)
        return len(unique_wallets)

    async def _check_honeypot(self, token_mint: str) -> bool:
        """
        Check if a token is a honeypot (can it be sold?).

        Strategy: Simulate a small sell via Jupiter quote API.
        If Jupiter can't find a route to sell, the token is likely a honeypot.

        IMPORTANT: If the API is unreachable (DNS fail, timeout, etc.),
        we return False (allow the trade) rather than blocking everything.
        Network issues != honeypot. Real safety comes from position sizing.

        Returns True if it's a CONFIRMED honeypot, False otherwise.
        """
        try:
            # SOL mint address
            sol_mint = "So11111111111111111111111111111111111111112"

            # Try to get a quote for selling a tiny amount of the token for SOL
            url = f"{self.settings.jupiter_base_url}/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": sol_mint,
                "amount": "1000000",  # Tiny amount (1 token with 6 decimals)
                "slippageBps": "1000",  # High slippage tolerance for test
            }

            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    # If we got a valid quote, the token can be sold
                    if data.get("outAmount") and int(data["outAmount"]) > 0:
                        return False  # NOT a honeypot — can sell
                    else:
                        logger.warning("honeypot_no_output", token=token_mint[:8])
                        return True  # No output amount = can't sell
                elif response.status in (400, 422):
                    # Jupiter explicitly rejected — likely honeypot or invalid token
                    logger.warning("honeypot_no_route", token=token_mint[:8], status=response.status)
                    return True
                else:
                    # Server error, rate limit, etc — not a honeypot signal
                    logger.warning("honeypot_check_inconclusive", token=token_mint[:8], status=response.status)
                    return False  # Don't block trades due to API issues

        except (aiohttp.ClientConnectorError, aiohttp.ClientError, OSError) as e:
            # Network/DNS issues — API is unreachable, NOT a honeypot signal
            logger.warning("honeypot_check_unreachable", token=token_mint[:8], error=str(e)[:80])
            return False  # Allow the trade — network issues != honeypot

        except Exception as e:
            logger.error("honeypot_check_error", error=str(e))
            return False  # Don't block trades on unexpected errors
