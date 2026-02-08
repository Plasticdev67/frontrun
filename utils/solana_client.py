"""
Solana Client Helper
====================
A thin wrapper around Solana RPC calls via Helius.

This module handles:
- Connecting to Solana via Helius RPC
- Loading the trading wallet from the private key
- Common RPC calls we'll use throughout the bot
- Rate limiting and error handling for API calls

Why Helius instead of public RPC?
- Public Solana RPC nodes are rate-limited and unreliable
- Helius provides enhanced transaction parsing (DAS API)
- Helius webhooks let us monitor wallets in real-time
- Worth every penny for a trading bot
"""

import base58
import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from utils.logger import get_logger

logger = get_logger(__name__)


class SolanaClient:
    """
    Async Solana client powered by Helius RPC.

    Usage:
        client = SolanaClient(helius_api_key="your_key")
        await client.initialize()
        balance = await client.get_sol_balance("wallet_address_here")
        await client.close()
    """

    def __init__(self, helius_api_key: str, wallet_private_key: str | None = None):
        self.helius_api_key = helius_api_key
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self.helius_api_url = f"https://api.helius.xyz/v0"
        self.session: aiohttp.ClientSession | None = None
        self.keypair: Keypair | None = None

        # Load the trading wallet if a private key is provided
        if wallet_private_key:
            try:
                secret_bytes = base58.b58decode(wallet_private_key)
                self.keypair = Keypair.from_bytes(secret_bytes)
                logger.info("wallet_loaded", address=str(self.keypair.pubkey()))
            except Exception as e:
                logger.error("wallet_load_failed", error=str(e))

    @property
    def wallet_address(self) -> str | None:
        """Get the bot's trading wallet address."""
        return str(self.keypair.pubkey()) if self.keypair else None

    async def initialize(self) -> None:
        """Create the HTTP session for making API calls."""
        self.session = aiohttp.ClientSession()
        logger.info("solana_client_initialized", rpc="helius")

    async def close(self) -> None:
        """Clean up the HTTP session."""
        if self.session:
            await self.session.close()

    # =========================================================================
    # Core RPC Calls
    # =========================================================================

    async def _rpc_call(self, method: str, params: list | None = None) -> dict:
        """
        Make a JSON-RPC call to the Solana node.

        This is the low-level method that all other RPC calls use.
        JSON-RPC is just a standard way to call functions over HTTP.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }
        async with self.session.post(self.rpc_url, json=payload) as response:
            data = await response.json()
            if "error" in data:
                logger.error("rpc_error", method=method, error=data["error"])
            return data

    async def get_sol_balance(self, address: str) -> float:
        """
        Get the SOL balance of a wallet.
        Returns balance in SOL (not lamports).

        1 SOL = 1,000,000,000 lamports (like dollars and cents, but with 9 decimals)
        """
        result = await self._rpc_call("getBalance", [address])
        lamports = result.get("result", {}).get("value", 0)
        return lamports / 1_000_000_000  # Convert lamports to SOL

    async def get_token_accounts(self, wallet_address: str) -> list[dict]:
        """
        Get all token accounts (SPL tokens) owned by a wallet.
        This tells us what tokens a wallet is holding and how much of each.
        """
        params = [
            wallet_address,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ]
        result = await self._rpc_call("getTokenAccountsByOwner", params)
        accounts = result.get("result", {}).get("value", [])
        return accounts

    async def get_transaction(self, signature: str) -> dict | None:
        """
        Get the full details of a transaction by its signature.
        A signature is Solana's version of a transaction hash (proof of a trade).
        """
        params = [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        result = await self._rpc_call("getTransaction", params)
        return result.get("result")

    async def get_signatures_for_address(
        self, address: str, limit: int = 100, before: str | None = None
    ) -> list[dict]:
        """
        Get recent transaction signatures for a wallet address.
        This is how we look up what a wallet has been doing recently.

        Args:
            address: The wallet to look up
            limit: Maximum number of transactions to return (max 1000)
            before: Only return transactions before this signature (for pagination)
        """
        options = {"limit": limit}
        if before:
            options["before"] = before
        result = await self._rpc_call("getSignaturesForAddress", [address, options])
        return result.get("result", [])

    # =========================================================================
    # Helius Enhanced APIs
    # =========================================================================

    async def get_parsed_transactions(self, signatures: list[str]) -> list[dict]:
        """
        Use Helius to parse raw transactions into human-readable format.

        Raw Solana transactions are nearly impossible to read — they're just
        lists of program instructions and account references. Helius parses
        them into clear events like "Wallet X swapped 1 SOL for 1000 PEPE".

        This is one of the main reasons we use Helius.
        """
        url = f"{self.helius_api_url}/transactions?api-key={self.helius_api_key}"
        payload = {"transactions": signatures}

        async with self.session.post(url, json=payload) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                logger.error("helius_parse_error", status=response.status, error=error_text)
                return []

    async def get_token_metadata(self, mint_address: str) -> dict | None:
        """
        Get token metadata (name, symbol, image) using Helius DAS API.
        DAS = Digital Asset Standard — Solana's way of storing token info.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAsset",
            "params": {"id": mint_address},
        }
        async with self.session.post(self.rpc_url, json=payload) as response:
            data = await response.json()
            return data.get("result")

    # =========================================================================
    # Wallet Analysis Helpers (used in Stage 2)
    # =========================================================================

    async def get_wallet_transaction_history(
        self, address: str, max_transactions: int = 500
    ) -> list[dict]:
        """
        Get a wallet's full recent transaction history, parsed by Helius.

        Fetches transactions in batches (Helius can parse up to 100 at a time)
        and returns them in chronological order.
        """
        all_parsed = []
        last_signature = None

        while len(all_parsed) < max_transactions:
            # Get the next batch of transaction signatures
            batch_size = min(100, max_transactions - len(all_parsed))
            signatures = await self.get_signatures_for_address(
                address, limit=batch_size, before=last_signature
            )

            if not signatures:
                break  # No more transactions

            # Parse them through Helius for readable data
            sig_list = [s["signature"] for s in signatures]
            parsed = await self.get_parsed_transactions(sig_list)
            all_parsed.extend(parsed)

            # Set up for the next page
            last_signature = signatures[-1]["signature"]

            logger.debug(
                "fetched_transactions",
                wallet=address[:8] + "...",
                batch=len(parsed),
                total=len(all_parsed),
            )

        return all_parsed
