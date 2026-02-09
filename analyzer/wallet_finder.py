"""
Wallet Finder
=============
For each winning token from Stage 1, this module finds WHO was early.

The process:
1. Take a top-performing token (e.g., $PEPE did 50x)
2. Look at the Birdeye "top traders" endpoint — who made the most profit?
3. Also trace back to early buyers using Helius transaction history
4. Cross-reference: if the same wallet was early on MULTIPLE winners,
   that's a strong signal they know what they're doing
5. Pass these wallet candidates to the WalletScorer for detailed analysis

Think of it like detective work:
- The token is the "crime scene" (a massive pump)
- We're finding the "suspects" (wallets that were suspiciously early)
- Then we check their "criminal record" (were they early on other winners too?)
"""

import asyncio
from datetime import datetime, timezone
from collections import defaultdict
from typing import Any

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.solana_client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)


class WalletFinder:
    """
    Finds promising wallet candidates by analyzing who profited from winning tokens.

    Usage:
        finder = WalletFinder(settings, db, solana)
        await finder.initialize()
        wallets = await finder.find_smart_wallets(tokens)
        await finder.close()
    """

    def __init__(self, settings: Settings, db: Database, solana: SolanaClient):
        self.settings = settings
        self.db = db
        self.solana = solana
        self.session: aiohttp.ClientSession | None = None
        self.birdeye_headers: dict = {}

        # Track wallet appearances across multiple tokens
        # Key: wallet_address, Value: list of tokens they were early on
        self.wallet_appearances: dict[str, list[dict]] = defaultdict(list)

    async def initialize(self) -> None:
        """Set up HTTP session."""
        self.session = aiohttp.ClientSession()
        self.birdeye_headers = {
            "X-API-KEY": self.settings.birdeye_api_key,
            "x-chain": "solana",
        }
        logger.info("wallet_finder_initialized")

    async def close(self) -> None:
        """Clean up."""
        if self.session:
            await self.session.close()

    async def find_smart_wallets(self, tokens: list[dict]) -> dict[str, list[dict]]:
        """
        Main method: Find wallets that were early/profitable on multiple winning tokens.

        Args:
            tokens: List of top-performing tokens from Stage 1

        Returns:
            Dictionary mapping wallet_address -> list of token trades
        """
        logger.info("wallet_search_starting", tokens_to_analyze=len(tokens))
        self.wallet_appearances.clear()

        for i, token in enumerate(tokens):
            symbol = token.get("symbol", "???")
            mint = token.get("mint_address")
            if not mint:
                continue

            logger.info(
                "analyzing_token",
                progress=f"{i+1}/{len(tokens)}",
                symbol=symbol,
                multiplier=f"{token.get('price_multiplier') or 0:.1f}x",
            )

            # Method 1: Get top traders from Birdeye (fast, structured data)
            top_traders = await self._get_birdeye_top_traders(mint)

            # Method 2: Get early buyers from transaction history (more thorough)
            early_buyers = await self._get_early_buyers(mint, symbol)

            # Combine both lists of wallets
            all_wallets = self._merge_wallet_data(top_traders, early_buyers)

            # Record which wallets appeared on this token
            for wallet_data in all_wallets:
                address = wallet_data.get("address")
                if address:
                    wallet_data["token_mint"] = mint
                    wallet_data["token_symbol"] = symbol
                    wallet_data["token_multiplier"] = token.get("price_multiplier") or 0
                    self.wallet_appearances[address].append(wallet_data)

                    # Save to database for later analysis
                    await self.db.insert_wallet_token_trade({
                        "wallet_address": address,
                        "token_mint": mint,
                        "token_symbol": symbol,
                        "buy_amount_sol": wallet_data.get("buy_amount_sol"),
                        "sell_amount_sol": wallet_data.get("sell_amount_sol"),
                        "pnl_sol": wallet_data.get("pnl_sol"),
                        "buy_price": wallet_data.get("buy_price"),
                        "sell_price": wallet_data.get("sell_price"),
                        "entry_rank": wallet_data.get("entry_rank"),
                        "first_buy_at": wallet_data.get("first_buy_at"),
                        "last_sell_at": wallet_data.get("last_sell_at"),
                    })

            logger.info(
                "token_analysis_complete",
                symbol=symbol,
                wallets_found=len(all_wallets),
            )

            # Rate limiting between tokens
            await asyncio.sleep(1)

        # Find wallets that appear across multiple winning tokens
        # These are the REAL smart money — not just lucky once
        # When we have very few tokens (< 5), include single-token wallets too
        # since there isn't enough data to require multi-token overlap
        min_appearances = 2 if len(tokens) >= 5 else 1
        multi_token_wallets = {
            addr: trades
            for addr, trades in self.wallet_appearances.items()
            if len(trades) >= min_appearances
        }

        logger.info(
            "wallet_search_complete",
            total_unique_wallets=len(self.wallet_appearances),
            multi_token_wallets=len(multi_token_wallets),
        )

        return multi_token_wallets

    async def _get_birdeye_top_traders(self, token_mint: str) -> list[dict]:
        """
        Get top traders for a token from Birdeye's API.

        Birdeye tracks who bought and sold each token and calculates their PnL.
        This is the fastest way to find profitable traders.
        """
        url = f"https://public-api.birdeye.so/trader/gainers-losers"
        params = {
            "address": token_mint,
            "time_frame": "30d",
            "sort_by": "PnL",
            "sort_type": "desc",
            "limit": 100,
        }

        try:
            async with self.session.get(url, headers=self.birdeye_headers, params=params) as response:
                if response.status != 200:
                    logger.debug("birdeye_traders_error", status=response.status)
                    return []

                data = await response.json()
                items = data.get("data", {}).get("items", [])

                traders = []
                for item in items:
                    # Only interested in profitable traders
                    pnl = item.get("pnl") or item.get("realized_pnl") or 0
                    if pnl <= 0:
                        continue

                    traders.append({
                        "address": item.get("owner") or item.get("address", ""),
                        "pnl_sol": pnl,
                        "buy_amount_sol": item.get("total_buy_amount") or item.get("buy_amount", 0),
                        "sell_amount_sol": item.get("total_sell_amount") or item.get("sell_amount", 0),
                        "source": "birdeye_top_traders",
                    })

                return traders

        except Exception as e:
            logger.error("birdeye_traders_exception", error=str(e))
            return []

    async def _get_early_buyers(self, token_mint: str, symbol: str) -> list[dict]:
        """
        Find the earliest buyers of a token by tracing transaction history.

        Uses Helius parsed transactions to find swap events where
        someone bought this specific token, then ranks by time
        (earliest = most interesting).
        """
        try:
            # Get recent transaction signatures for this token's mint address
            signatures = await self.solana.get_signatures_for_address(
                token_mint, limit=200
            )

            if not signatures:
                return []

            # Parse them through Helius for readable data
            sig_list = [s["signature"] for s in signatures]

            # Process in batches of 100 (Helius limit)
            all_parsed = []
            for i in range(0, len(sig_list), 100):
                batch = sig_list[i:i+100]
                parsed = await self.solana.get_parsed_transactions(batch)
                all_parsed.extend(parsed)
                await asyncio.sleep(0.5)

            # Extract buyers from parsed transactions
            buyers = []
            seen_wallets = set()
            rank = 0

            for tx in all_parsed:
                # Look for SWAP events in the parsed transaction
                tx_type = tx.get("type", "")
                if tx_type != "SWAP":
                    continue

                # Check if this swap involved our target token
                token_transfers = tx.get("tokenTransfers", [])
                fee_payer = tx.get("feePayer", "")

                for transfer in token_transfers:
                    mint = transfer.get("mint", "")
                    if mint != token_mint:
                        continue

                    # This is a buy if the token was transferred TO the fee payer
                    to_addr = transfer.get("toUserAccount", "")
                    if to_addr == fee_payer and fee_payer not in seen_wallets:
                        rank += 1
                        seen_wallets.add(fee_payer)

                        amount = transfer.get("tokenAmount", 0)
                        timestamp = tx.get("timestamp")

                        buyers.append({
                            "address": fee_payer,
                            "entry_rank": rank,
                            "first_buy_at": datetime.fromtimestamp(
                                timestamp, tz=timezone.utc
                            ).isoformat() if timestamp else None,
                            "buy_amount_tokens": amount,
                            "source": "helius_early_buyers",
                        })

            logger.debug(
                "early_buyers_found",
                symbol=symbol,
                count=len(buyers),
                transactions_scanned=len(all_parsed),
            )

            return buyers

        except Exception as e:
            logger.error("early_buyers_error", symbol=symbol, error=str(e))
            return []

    def _merge_wallet_data(
        self, top_traders: list[dict], early_buyers: list[dict]
    ) -> list[dict]:
        """
        Merge wallet data from Birdeye and Helius sources.

        If the same wallet appears in both (profitable AND early), combine
        the data. This is the strongest signal — someone who was early AND
        made money.
        """
        merged = {}

        # Start with top traders (they have PnL data)
        for trader in top_traders:
            addr = trader.get("address", "")
            if addr:
                merged[addr] = trader

        # Add/update with early buyer data (they have timing data)
        for buyer in early_buyers:
            addr = buyer.get("address", "")
            if not addr:
                continue

            if addr in merged:
                # This wallet was both profitable AND early — jackpot!
                merged[addr].update({
                    "entry_rank": buyer.get("entry_rank"),
                    "first_buy_at": buyer.get("first_buy_at"),
                    "was_early_and_profitable": True,
                })
            else:
                merged[addr] = buyer

        return list(merged.values())
