"""
Jupiter Client
===============
Handles all swap execution through the Jupiter Aggregator.

Jupiter is the #1 DEX aggregator on Solana. It finds the best price
across ALL Solana DEXs (Raydium, Orca, Meteora, etc.) and routes your
swap through the optimal path. Think of it like a "Google Flights"
for token swaps — it compares all options and picks the cheapest.

The swap process:
1. Get a QUOTE: "I want to swap 0.01 SOL for token X. What's the best price?"
2. Get the TRANSACTION: Jupiter builds the actual Solana transaction
3. SIGN it with our wallet's private key
4. SEND it to the Solana network
5. CONFIRM it landed on-chain

We never interact with DEXs directly — Jupiter handles all the complexity.
"""

import base64
from typing import Any

import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config.settings import Settings
from utils.logger import get_logger

logger = get_logger(__name__)

# SOL's special mint address on Solana
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterClient:
    """
    Client for the Jupiter V6 Swap API.

    Usage:
        jupiter = JupiterClient(settings, keypair, session)
        quote = await jupiter.get_quote(SOL_MINT, token_mint, amount_lamports)
        tx_sig = await jupiter.execute_swap(quote)
    """

    def __init__(
        self,
        settings: Settings,
        keypair: Keypair | None,
        session: aiohttp.ClientSession,
        rpc_url: str = "",
    ):
        self.settings = settings
        self.keypair = keypair
        self.session = session
        self.rpc_url = rpc_url
        self.base_url = settings.jupiter_base_url

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int | None = None,
    ) -> dict | None:
        """
        Get a swap quote from Jupiter.

        Args:
            input_mint: Token you're selling (e.g., SOL_MINT for SOL)
            output_mint: Token you're buying (e.g., a memecoin's mint address)
            amount: Amount to sell in smallest units (lamports for SOL)
            slippage_bps: Max slippage tolerance (overrides default if set)

        Returns:
            Quote data including expected output amount, price impact, and route.
            Returns None if no route found.
        """
        slippage = slippage_bps or self.settings.slippage_bps

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }

        try:
            url = f"{self.base_url}/quote"
            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    quote = await response.json()

                    # Log the quote details
                    in_amount = int(quote.get("inAmount", 0))
                    out_amount = int(quote.get("outAmount", 0))
                    price_impact = quote.get("priceImpactPct", "0")

                    logger.info(
                        "quote_received",
                        input_amount=in_amount,
                        output_amount=out_amount,
                        price_impact=f"{float(price_impact):.2f}%",
                        route=quote.get("routePlan", [{}])[0].get("swapInfo", {}).get("label", "unknown"),
                    )

                    return quote
                else:
                    error = await response.text()
                    logger.error("quote_failed", status=response.status, error=error)
                    return None

        except Exception as e:
            logger.error("quote_error", error=str(e))
            return None

    async def execute_swap(self, quote: dict) -> str | None:
        """
        Execute a swap based on a Jupiter quote.

        This is where real money moves. The process:
        1. Send the quote to Jupiter to build a transaction
        2. Sign the transaction with our private key
        3. Send it to the Solana network
        4. Return the transaction signature (proof of trade)

        Returns the transaction signature, or None if it failed.
        """
        if not self.keypair:
            logger.error("no_keypair", note="Cannot execute swap without wallet private key")
            return None

        try:
            # Step 1: Get the swap transaction from Jupiter
            swap_url = f"{self.base_url}/swap"
            swap_body = {
                "quoteResponse": quote,
                "userPublicKey": str(self.keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": self.settings.priority_fee_microlamports,
                "dynamicComputeUnitLimit": True,
            }

            async with self.session.post(swap_url, json=swap_body) as response:
                if response.status != 200:
                    error = await response.text()
                    logger.error("swap_tx_build_failed", status=response.status, error=error)
                    return None

                swap_data = await response.json()

            # Step 2: Decode and sign the transaction
            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                logger.error("no_swap_transaction_returned")
                return None

            tx_bytes = base64.b64decode(swap_tx_b64)
            transaction = VersionedTransaction.from_bytes(tx_bytes)

            # Sign it with our private key — this authorizes the swap
            signed_tx = VersionedTransaction(transaction.message, [self.keypair])
            signed_bytes = bytes(signed_tx)

            # Step 3: Send the signed transaction to the Solana network
            tx_signature = await self._send_transaction(signed_bytes)

            if tx_signature:
                logger.info("swap_sent", tx_signature=tx_signature)

            return tx_signature

        except Exception as e:
            logger.error("swap_execution_error", error=str(e), type=type(e).__name__)
            return None

    async def _send_transaction(self, signed_bytes: bytes) -> str | None:
        """
        Send a signed transaction to the Solana network via our RPC node.

        We use maxRetries=3 and skipPreflight=True for speed:
        - maxRetries: Solana will retry sending if it doesn't land immediately
        - skipPreflight: Skips simulation, sends directly (faster but less safe)
        """
        encoded = base64.b64encode(signed_bytes).decode("utf-8")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                encoded,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 3,
                    "preflightCommitment": "confirmed",
                },
            ],
        }

        try:
            async with self.session.post(self.rpc_url, json=payload) as response:
                data = await response.json()

                if "error" in data:
                    logger.error("send_tx_error", error=data["error"])
                    return None

                return data.get("result")

        except Exception as e:
            logger.error("send_tx_exception", error=str(e))
            return None

    async def confirm_transaction(self, signature: str, timeout: int = 30) -> bool:
        """
        Wait for a transaction to be confirmed on-chain.

        A transaction isn't "done" until it's confirmed by the network.
        We poll the RPC to check the status.

        Returns True if confirmed, False if it timed out or failed.
        """
        import asyncio

        for _ in range(timeout):
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[signature], {"searchTransactionHistory": True}],
                }

                async with self.session.post(self.rpc_url, json=payload) as response:
                    data = await response.json()
                    statuses = data.get("result", {}).get("value", [])

                    if statuses and statuses[0]:
                        status = statuses[0]
                        if status.get("confirmationStatus") in ("confirmed", "finalized"):
                            if status.get("err"):
                                logger.error("tx_confirmed_with_error", signature=signature, error=status["err"])
                                return False
                            logger.info("tx_confirmed", signature=signature)
                            return True

            except Exception as e:
                logger.debug("confirm_check_error", error=str(e))

            await asyncio.sleep(1)

        logger.warning("tx_confirmation_timeout", signature=signature)
        return False
