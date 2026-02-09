"""
FOMO On-Chain Wallet Discoverer
================================
Discovers FOMO platform user wallets by analyzing on-chain transactions.

How it works:
Every FOMO trade goes through a Privy relayer wallet (pays gas for users) and
sends a USDC fee to FOMO's fee wallet. By scanning the relayer's transaction
history, we can identify every wallet that has traded through FOMO.

Key infrastructure addresses:
- Fee Wallet:  R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX  (receives USDC fee)
- Relayer:     AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51  (Privy, pays gas)

Usage:
    discoverer = FomoDiscoverer(settings, db, solana)
    wallets = await discoverer.discover(max_transactions=1000, enrich=True)
"""

import asyncio
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# FOMO platform infrastructure addresses — never import these as traders
FOMO_FEE_WALLET = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"
FOMO_RELAYER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"

# Known infrastructure wallets to exclude (routers, programs, etc.)
INFRA_WALLETS = {
    FOMO_FEE_WALLET,
    FOMO_RELAYER,
    "11111111111111111111111111111111",                     # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",        # Token Program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",       # Jupiter v6
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",      # ATA Program
    "ComputeBudget111111111111111111111111111111",          # Compute Budget
}


def _float(val, default=0.0):
    """Safely convert GMGN value to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class FomoDiscoverer:
    """
    Discovers FOMO platform traders by scanning the Privy relayer's on-chain
    transaction history via Helius Enhanced API.
    """

    def __init__(self, settings, db, solana):
        self.settings = settings
        self.db = db
        self.solana = solana

    async def discover(
        self,
        max_transactions: int = 1000,
        enrich: bool = True,
    ) -> list[dict]:
        """
        Discover FOMO wallets from on-chain data.

        Steps:
        1. Fetch relayer transactions from Helius
        2. Extract unique user wallets from token transfers
        3. Filter out infrastructure wallets
        4. Optionally enrich via GMGN
        5. Save to both fomo_traders and wallets tables

        Args:
            max_transactions: How many relayer transactions to scan
            enrich: Whether to enrich wallets via GMGN (slower but better data)

        Returns:
            List of discovered wallet dicts
        """
        print(f"\n  Scanning FOMO relayer transactions...")
        print(f"  Relayer: {FOMO_RELAYER[:8]}...{FOMO_RELAYER[-4:]}")
        print(f"  Fee Wallet: {FOMO_FEE_WALLET[:8]}...{FOMO_FEE_WALLET[-4:]}")

        # Step 1: Fetch relayer transaction history from Helius
        raw_wallets = await self._scan_relayer_transactions(max_transactions)

        if not raw_wallets:
            print("  No FOMO wallets found. The relayer may have changed.")
            return []

        print(f"  Found {len(raw_wallets)} unique FOMO wallets on-chain")

        # Step 2: Filter out wallets we already have in DB
        new_wallets = []
        existing_count = 0
        for addr in raw_wallets:
            existing = await self.db.get_fomo_trader(addr)
            if existing:
                existing_count += 1
            else:
                new_wallets.append(addr)

        print(f"  Already tracked: {existing_count} | New: {len(new_wallets)}")

        if not new_wallets:
            print("  No new wallets to import.")
            return []

        # Step 3: Enrich via GMGN and save
        if enrich:
            return await self._enrich_and_save(new_wallets)
        else:
            return await self._save_without_enrichment(new_wallets)

    async def _scan_relayer_transactions(self, max_transactions: int) -> list[str]:
        """
        Scan the FOMO relayer's transaction history and extract user wallet
        addresses from the account keys of each transaction.

        The relayer (Privy wallet) pays gas for every FOMO user trade.
        Each transaction's account list includes the user's wallet address.
        We identify user wallets by filtering out known infrastructure addresses.
        """
        all_user_wallets = set()
        last_sig = None
        scanned = 0
        batch_num = 0

        while scanned < max_transactions:
            batch_num += 1
            batch_size = min(100, max_transactions - scanned)

            # Get transaction signatures for the relayer
            sigs = await self.solana.get_signatures_for_address(
                FOMO_RELAYER, limit=batch_size, before=last_sig
            )

            if not sigs:
                break

            # Parse transactions through Helius for enriched data
            sig_list = [s["signature"] for s in sigs]
            parsed_txs = await self.solana.get_parsed_transactions(sig_list)

            for tx in parsed_txs:
                wallets = self._extract_user_wallets(tx)
                all_user_wallets.update(wallets)

            scanned += len(sigs)
            last_sig = sigs[-1]["signature"]

            print(f"  Batch {batch_num}: scanned {scanned} txs, "
                  f"found {len(all_user_wallets)} unique wallets so far")

            # Rate limiting — Helius has generous limits but let's be polite
            await asyncio.sleep(0.3)

        return list(all_user_wallets)

    def _extract_user_wallets(self, parsed_tx: dict) -> set[str]:
        """
        Extract user wallet addresses from a parsed Helius transaction.

        Strategy: Look at all accounts involved in the transaction.
        Filter out known infrastructure (programs, relayer, fee wallet, etc.).
        The remaining accounts that look like regular wallets are FOMO users.

        We also check token transfers — if a wallet receives swapped tokens,
        it's definitely a user wallet.
        """
        user_wallets = set()

        # Method 1: Check token transfers for the fee wallet pattern
        # Every FOMO trade sends USDC to the fee wallet — the source is the user
        token_transfers = parsed_tx.get("tokenTransfers", [])
        for transfer in token_transfers:
            to_addr = transfer.get("toUserAccount", "")
            from_addr = transfer.get("fromUserAccount", "")

            # If sending to fee wallet, the 'from' is likely a router/intermediate
            # But the fee payer or other accounts will be the user
            if to_addr == FOMO_FEE_WALLET:
                # The fromUserAccount for fee transfers is usually a router
                # The actual user is in the account list — we'll pick them up below
                pass

            # If receiving swapped tokens and not infrastructure, it's a user
            if from_addr and from_addr not in INFRA_WALLETS:
                # Could be user or router — we'll validate below
                pass
            if to_addr and to_addr not in INFRA_WALLETS and to_addr != FOMO_FEE_WALLET:
                user_wallets.add(to_addr)

        # Method 2: Check native SOL transfers
        native_transfers = parsed_tx.get("nativeTransfers", [])
        for transfer in native_transfers:
            from_addr = transfer.get("fromUserAccount", "")
            to_addr = transfer.get("toUserAccount", "")

            # Relayer sends SOL to user wallets (wrapped SOL for trades)
            if from_addr == FOMO_RELAYER and to_addr not in INFRA_WALLETS:
                user_wallets.add(to_addr)

        # Method 3: Check accountData for signers that aren't infrastructure
        account_data = parsed_tx.get("accountData", [])
        for acc in account_data:
            addr = acc.get("account", "")
            # Fee payers and signers who aren't the relayer are users
            if addr and addr not in INFRA_WALLETS and len(addr) >= 32:
                # Additional check: must be in the native/token transfer flow
                # to avoid picking up random program accounts
                pass

        # Method 4: The fee payer — if it's the relayer, look at other signers
        fee_payer = parsed_tx.get("feePayer", "")
        if fee_payer == FOMO_RELAYER:
            # Transaction description often contains the user wallet
            desc = parsed_tx.get("description", "")
            # The description format is usually like:
            # "AgmL... swapped X SOL for Y TOKEN"
            # But the actual user is in the instructions
            pass

        # Clean up: remove any remaining infrastructure
        user_wallets -= INFRA_WALLETS
        # Remove very short addresses (likely parsing artifacts)
        user_wallets = {w for w in user_wallets if len(w) >= 32}

        return user_wallets

    async def _enrich_and_save(self, addresses: list[str]) -> list[dict]:
        """Enrich wallet addresses via GMGN and save to database."""
        from discovery.gmgn_client import GMGNClient

        gmgn = GMGNClient(
            cf_clearance=self.settings.gmgn_cf_clearance,
            cf_bm=self.settings.gmgn_cf_bm,
        )

        saved = []
        total = len(addresses)

        print(f"\n  Enriching {total} wallets via GMGN...")

        for i, addr in enumerate(addresses):
            stats = await gmgn.get_wallet_stats(addr)

            profit_30d = _float(stats.get("realized_profit_30d"))
            winrate = stats.get("winrate")
            tags = stats.get("tags") or []
            if isinstance(tags, str):
                import json
                try:
                    tags = json.loads(tags)
                except (ValueError, TypeError):
                    tags = []

            realized_profit = _float(stats.get("realized_profit"))
            sol_balance = _float(stats.get("sol_balance"))
            buy_30d = int(_float(stats.get("buy_30d")))
            sell_30d = int(_float(stats.get("sell_30d")))

            # Save to fomo_traders table
            await self.db.upsert_fomo_trader({
                "wallet_address": addr,
                "platform": "fomo",
                "pnl_30d_usd": profit_30d,
                "is_tracked": True,
                "notes": "auto-discovered on-chain",
            })

            # Save to wallets table with GMGN enrichment + auto-monitor
            wallet_data = {
                "address": addr,
                "source": "fomo",
                "total_score": 60,  # FOMO traders start higher
                "gmgn_realized_profit_usd": realized_profit,
                "gmgn_profit_30d_usd": profit_30d,
                "gmgn_sol_balance": sol_balance,
                "gmgn_winrate": _float(winrate) if winrate is not None else None,
                "gmgn_buy_30d": buy_30d,
                "gmgn_sell_30d": sell_30d,
                "gmgn_tags": tags,
                "is_monitored": True,
            }
            await self.db.upsert_wallet(wallet_data)

            wr_display = f"{_float(winrate)*100:.0f}%" if winrate is not None else "—"
            saved.append({
                "address": addr,
                "profit_30d": profit_30d,
                "winrate": winrate,
                "sol_balance": sol_balance,
                "tags": tags,
            })

            # Progress report every 10 wallets
            if (i + 1) % 10 == 0 or (i + 1) == total:
                print(f"  Enriched {i+1}/{total} wallets...")

            # Rate limiting for GMGN
            await asyncio.sleep(0.5)

        gmgn.close()
        return saved

    async def _save_without_enrichment(self, addresses: list[str]) -> list[dict]:
        """Save wallets to DB without GMGN enrichment (fast mode)."""
        saved = []
        for addr in addresses:
            await self.db.upsert_fomo_trader({
                "wallet_address": addr,
                "platform": "fomo",
                "is_tracked": True,
                "notes": "auto-discovered on-chain (not enriched)",
            })

            await self.db.upsert_wallet({
                "address": addr,
                "source": "fomo",
                "total_score": 50,  # Lower score without GMGN data
                "is_monitored": True,
            })

            saved.append({"address": addr})

        return saved
