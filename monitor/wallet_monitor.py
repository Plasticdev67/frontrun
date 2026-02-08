"""
Wallet Monitor
==============
Watches smart wallets in real-time for new token purchases.

Two monitoring strategies (with automatic fallback):

1. Helius Webhooks (preferred — fastest)
   - Helius sends us a notification whenever a monitored wallet transacts
   - Near-instant detection (1-3 seconds)
   - Requires a publicly accessible webhook URL (needs a server)

2. RPC Polling (fallback — reliable everywhere)
   - We periodically check each wallet's recent transactions
   - Detection speed depends on poll interval (5-15 seconds)
   - Works anywhere, no webhook URL needed

For v1, we'll use polling since it works locally without a server.
When you deploy to a VPS, we can switch to webhooks for faster detection.

The monitor runs as an infinite loop — it never stops until you tell it to.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.solana_client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)


class WalletMonitor:
    """
    Real-time monitoring of smart wallets for new token purchases.

    Usage:
        monitor = WalletMonitor(settings, db, solana, on_signal=callback)
        await monitor.initialize()
        await monitor.start()  # Runs forever
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        solana: SolanaClient,
        on_signal: Any = None,
    ):
        self.settings = settings
        self.db = db
        self.solana = solana
        # Callback function called when a smart wallet buys a new token
        # Signature: async def on_signal(signal_data: dict) -> None
        self.on_signal = on_signal

        # Track the last known transaction for each wallet
        # So we only process NEW transactions, not old ones
        self.last_signatures: dict[str, str] = {}

        # How often to poll (in seconds)
        self.poll_interval = 5

        # Track tokens we've already seen signals for (avoid duplicates)
        self.recent_signals: set[str] = set()

    async def initialize(self) -> None:
        """Load monitored wallets and their last known state."""
        wallets = await self.db.get_monitored_wallets()
        logger.info("monitor_initialized", wallets_to_watch=len(wallets))

        # Get the most recent transaction signature for each wallet
        # This is our "starting point" — we'll only look at transactions after this
        for wallet in wallets:
            address = wallet["address"]
            try:
                sigs = await self.solana.get_signatures_for_address(address, limit=1)
                if sigs:
                    self.last_signatures[address] = sigs[0]["signature"]
            except Exception as e:
                logger.warning("init_sig_error", wallet=address[:8] + "...", error=str(e))

            await asyncio.sleep(0.2)  # Rate limiting during startup

        logger.info("monitor_ready", wallets_loaded=len(self.last_signatures))

    async def start(self) -> None:
        """
        Start the monitoring loop. Runs forever until cancelled.
        Checks all monitored wallets for new transactions every poll_interval seconds.
        """
        logger.info("monitor_started", poll_interval=f"{self.poll_interval}s")

        while True:
            try:
                # Don't do anything if trading is paused
                if self.settings.trading_paused:
                    logger.debug("monitor_paused")
                    await asyncio.sleep(self.poll_interval)
                    continue

                # Check each monitored wallet for new activity
                wallets = await self.db.get_monitored_wallets()

                for wallet in wallets:
                    try:
                        await self._check_wallet(wallet)
                    except Exception as e:
                        logger.error(
                            "wallet_check_error",
                            wallet=wallet["address"][:8] + "...",
                            error=str(e),
                        )

                    # Small delay between wallets to avoid rate limits
                    await asyncio.sleep(0.5)

                # Wait before next poll cycle
                await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                logger.info("monitor_stopping")
                break
            except Exception as e:
                logger.error("monitor_error", error=str(e))
                await asyncio.sleep(self.poll_interval)

    async def _check_wallet(self, wallet: dict) -> None:
        """
        Check a single wallet for new transactions since we last looked.
        If we find a new token buy, generate a signal.
        """
        address = wallet["address"]
        last_sig = self.last_signatures.get(address)

        # Get recent transactions (newer than our last known)
        signatures = await self.solana.get_signatures_for_address(address, limit=10)
        if not signatures:
            return

        # Find transactions we haven't seen yet
        new_sigs = []
        for sig_info in signatures:
            sig = sig_info["signature"]
            if sig == last_sig:
                break  # We've reached transactions we already processed
            new_sigs.append(sig)

        if not new_sigs:
            return  # No new activity

        # Update our "last seen" marker
        self.last_signatures[address] = signatures[0]["signature"]

        # Parse the new transactions through Helius
        parsed = await self.solana.get_parsed_transactions(new_sigs)

        for tx in parsed:
            signal = self._extract_buy_signal(tx, wallet)
            if signal:
                await self._handle_signal(signal)

    def _extract_buy_signal(self, tx: dict, wallet: dict) -> dict | None:
        """
        Analyze a parsed transaction to determine if it's a token buy.

        We're looking for SWAP transactions where the wallet traded
        SOL (or USDC) for a new token. This means they're buying into
        something — which is the signal we want to copy.

        Returns signal data if it's a buy, None otherwise.
        """
        tx_type = tx.get("type", "")

        # Only interested in swaps (token buys/sells)
        if tx_type != "SWAP":
            return None

        fee_payer = tx.get("feePayer", "")
        wallet_address = wallet["address"]

        # Make sure this transaction was initiated by our monitored wallet
        if fee_payer != wallet_address:
            return None

        # Look at token transfers to figure out what was bought
        token_transfers = tx.get("tokenTransfers", [])
        native_transfers = tx.get("nativeTransfers", [])

        # Find the token that was received (bought)
        bought_token = None
        sol_spent = 0

        for transfer in token_transfers:
            to_addr = transfer.get("toUserAccount", "")
            from_addr = transfer.get("fromUserAccount", "")
            mint = transfer.get("mint", "")

            # Skip SOL-wrapped token (that's the payment, not the buy)
            if mint == "So11111111111111111111111111111111111111112":
                continue

            # Token received by the wallet = they bought it
            if to_addr == wallet_address:
                bought_token = {
                    "mint": mint,
                    "amount": transfer.get("tokenAmount", 0),
                }

        # Calculate SOL spent from native transfers
        for transfer in native_transfers:
            if transfer.get("fromUserAccount") == wallet_address:
                sol_spent += transfer.get("amount", 0) / 1_000_000_000  # lamports to SOL

        if not bought_token:
            return None  # Not a buy, or couldn't parse it

        token_mint = bought_token["mint"]

        # Dedup: don't signal the same token from the same wallet twice in a row
        dedup_key = f"{wallet_address}:{token_mint}"
        if dedup_key in self.recent_signals:
            return None
        self.recent_signals.add(dedup_key)

        # Limit dedup set size to prevent memory leak
        if len(self.recent_signals) > 1000:
            self.recent_signals = set(list(self.recent_signals)[-500:])

        return {
            "wallet_address": wallet_address,
            "wallet_score": wallet.get("total_score", 0),
            "token_mint": token_mint,
            "token_amount": bought_token["amount"],
            "sol_spent": sol_spent,
            "signal_type": "buy",
            "tx_signature": tx.get("signature", ""),
            "timestamp": tx.get("timestamp"),
            "confidence": self._calculate_confidence(wallet, sol_spent),
        }

    def _calculate_confidence(self, wallet: dict, sol_spent: float) -> float:
        """
        Calculate how confident we are in this signal (0.0 to 1.0).

        Higher confidence when:
        - Wallet has a high score
        - Wallet spent a significant amount of SOL (they're serious)
        - Wallet has a strong track record
        """
        confidence = 0.5  # Baseline

        # Wallet score contributes up to 0.3
        score = wallet.get("total_score", 0)
        confidence += (score / 100) * 0.3

        # SOL amount contributes up to 0.2
        # If they're spending 5+ SOL, they're serious
        if sol_spent >= 5:
            confidence += 0.2
        elif sol_spent >= 1:
            confidence += 0.15
        elif sol_spent >= 0.5:
            confidence += 0.1

        return min(1.0, confidence)

    async def _handle_signal(self, signal: dict) -> None:
        """
        Process a new buy signal from a monitored wallet.
        Saves it to the database and triggers the callback (which leads to trade execution).
        """
        logger.info(
            "SIGNAL_DETECTED",
            wallet=signal["wallet_address"][:8] + "...",
            token=signal["token_mint"][:8] + "...",
            sol_spent=f"{signal['sol_spent']:.4f} SOL",
            wallet_score=signal["wallet_score"],
            confidence=f"{signal['confidence']:.2f}",
        )

        # Save signal to database
        signal_id = await self.db.insert_signal({
            "wallet_address": signal["wallet_address"],
            "token_mint": signal["token_mint"],
            "token_symbol": None,  # Will be enriched by the executor
            "signal_type": signal["signal_type"],
            "wallet_score": signal["wallet_score"],
            "confidence": signal["confidence"],
        })
        signal["signal_id"] = signal_id

        # Trigger the callback (trade executor or alert system)
        if self.on_signal:
            try:
                await self.on_signal(signal)
            except Exception as e:
                logger.error("signal_callback_error", error=str(e))

    async def add_wallet(self, address: str) -> None:
        """Add a new wallet to the monitoring list at runtime."""
        # Get its latest transaction as starting point
        sigs = await self.solana.get_signatures_for_address(address, limit=1)
        if sigs:
            self.last_signatures[address] = sigs[0]["signature"]
        logger.info("wallet_added_to_monitor", address=address[:8] + "...")

    async def remove_wallet(self, address: str) -> None:
        """Remove a wallet from the monitoring list at runtime."""
        self.last_signatures.pop(address, None)
        logger.info("wallet_removed_from_monitor", address=address[:8] + "...")
