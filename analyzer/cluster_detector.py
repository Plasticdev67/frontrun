"""
Wallet Cluster Detector
=======================
Detects groups of wallets controlled by the same operator.

The core insight: many top Solana traders use multiple wallets.
- A "public" wallet appears on Birdeye leaderboards
- "Side" wallets quietly accumulate tokens BEFORE the public wallet buys
- When the public wallet buys, copiers pile in, price pumps
- The side wallets dump at the higher price

This module traces connections between wallets to find these clusters:
1. Funding Source Analysis — who sent SOL to this wallet?
2. Transfer Pattern Detection — which wallets send tokens/SOL back and forth?
3. Timing Correlation — does wallet A consistently buy before wallet B?
4. Token Overlap — do they trade the same obscure tokens?

The side wallets are the REAL alpha — they buy before the crowd.

Usage:
    python main.py --clusters    # runs after --analyze
"""

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config.settings import Settings
from database.db import Database
from utils.solana_client import SolanaClient
from utils.logger import get_logger

logger = get_logger(__name__)

# Known exchange hot wallets — exclude these from funding analysis
# (Many wallets are funded from exchanges, that doesn't make them related)
KNOWN_EXCHANGES = {
    "5tzFkiKscjHb5gRMRhMkDw98JhxTFLYT7A3cCp1qhxgR",  # Binance
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # FTX (inactive)
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",  # Coinbase
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ",  # OKX
}


class ClusterDetector:
    """
    Detects wallet clusters around known smart wallets.

    For each seed wallet (from leaderboards/scoring), it:
    1. Traces funding sources (who sent SOL to this wallet)
    2. Looks for token transfer patterns between connected wallets
    3. Checks timing correlation (who buys first on the same tokens)
    4. Checks token overlap (shared obscure tokens)
    5. Scores relationships and identifies side wallets
    """

    # Configuration
    MAX_TX_HISTORY = 200          # Max transactions per wallet
    FUNDING_DEPTH = 2             # Levels deep to trace funding
    MIN_TRANSFER_SOL = 0.01       # Ignore dust transfers
    MIN_TIMING_SAMPLES = 3        # Need 3+ shared tokens for timing
    TIMING_WINDOW_SECONDS = 1800  # 30 min max gap between buys
    MIN_TOKEN_OVERLAP = 3         # Need 3+ shared obscure tokens
    MIN_CONFIDENCE = 0.3          # Below this, skip
    BATCH_DELAY = 0.5             # Rate limit between API calls
    MAX_CANDIDATES = 15           # Max candidate wallets per seed

    def __init__(self, settings: Settings, db: Database, solana: SolanaClient):
        self.settings = settings
        self.db = db
        self.solana = solana
        self.session: aiohttp.ClientSession | None = None
        self._tx_cache: dict[str, list[dict]] = {}  # Avoids re-fetching same wallet

    async def initialize(self) -> None:
        self.session = aiohttp.ClientSession()
        logger.info("cluster_detector_initialized")

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    # =========================================================================
    # Main Entry Point
    # =========================================================================

    async def detect_clusters(self, seed_wallets: list[dict]) -> list[dict]:
        """
        Analyze each seed wallet and find connected wallet clusters.

        Args:
            seed_wallets: List of wallet dicts (must have "address" key)

        Returns:
            List of cluster result dicts with members and side wallets
        """
        clusters = []
        self._tx_cache = {}

        logger.info("cluster_detection_starting", seed_count=len(seed_wallets))

        for i, seed in enumerate(seed_wallets):
            address = seed["address"]
            score = seed.get("total_score", 0)

            # Skip if already analyzed
            existing = await self.db.get_cluster_by_seed(address)
            if existing:
                logger.debug("cluster_already_analyzed", wallet=address[:8])
                continue

            logger.info(
                "analyzing_seed_wallet",
                wallet=address[:8],
                score=f"{score:.0f}",
                progress=f"{i + 1}/{len(seed_wallets)}",
            )

            try:
                cluster = await self._analyze_single_wallet(address)
                if cluster and cluster.get("members"):
                    clusters.append(cluster)
            except Exception as e:
                logger.warning("cluster_analysis_failed", wallet=address[:8], error=str(e))
                continue

            # Rate limit between seed wallets
            await asyncio.sleep(self.BATCH_DELAY)

        # Clear cache
        self._tx_cache = {}

        # Print summary
        self._print_cluster_report(clusters)

        return clusters

    async def _analyze_single_wallet(self, seed_address: str) -> dict | None:
        """Run all 4 analyses on a single seed wallet and save results."""

        # Step 1: Find funding sources
        funding_links = await self._analyze_funding_sources(seed_address)
        candidate_set = {
            link["address"]
            for link in funding_links
            if link["address"] not in KNOWN_EXCHANGES
        }

        if not candidate_set:
            logger.debug("no_candidates_found", wallet=seed_address[:8])
            return None

        # Cap candidates to avoid explosion
        if len(candidate_set) > self.MAX_CANDIDATES:
            # Keep only the ones with highest SOL volume
            by_vol = sorted(funding_links, key=lambda x: x.get("total_sol", 0), reverse=True)
            candidate_set = {
                link["address"]
                for link in by_vol[:self.MAX_CANDIDATES]
                if link["address"] not in KNOWN_EXCHANGES
            }

        logger.info("candidates_found", wallet=seed_address[:8], count=len(candidate_set))

        # Step 2: Check transfer patterns
        transfer_data = await self._analyze_transfer_patterns(seed_address, candidate_set)

        # Step 3: Check timing correlation
        timing_data = await self._analyze_timing_correlation(seed_address, candidate_set)

        # Step 4: Check token overlap
        overlap_data = await self._analyze_token_overlap(seed_address, candidate_set)

        # Merge all evidence per candidate
        evidence_map: dict[str, dict] = defaultdict(lambda: {
            "funding": None, "transfers": None, "timing": None, "overlap": None
        })

        for link in funding_links:
            if link["address"] in candidate_set:
                evidence_map[link["address"]]["funding"] = link

        for td in transfer_data:
            evidence_map[td["address"]]["transfers"] = td

        for td in timing_data:
            evidence_map[td["address"]]["timing"] = td

        for od in overlap_data:
            evidence_map[od["address"]]["overlap"] = od

        # Score and classify each candidate
        members = []
        for addr, evidence in evidence_map.items():
            confidence = self._score_relationship(evidence)
            if confidence < self.MIN_CONFIDENCE:
                continue

            is_side = self._classify_side_wallet(evidence)
            lead_time = 0
            if evidence["timing"] and evidence["timing"].get("avg_lead_seconds", 0) > 0:
                lead_time = evidence["timing"]["avg_lead_seconds"]

            # Determine primary relationship type
            rel_type = self._primary_relationship(evidence)

            members.append({
                "wallet_address": addr,
                "relationship_type": rel_type,
                "is_side_wallet": is_side,
                "confidence": round(confidence, 3),
                "avg_lead_time_seconds": round(lead_time, 1),
                "evidence": json.dumps({
                    k: v for k, v in {
                        "funding_sol": evidence["funding"]["total_sol"] if evidence["funding"] else None,
                        "funding_direction": evidence["funding"]["direction"] if evidence["funding"] else None,
                        "transfer_count": evidence["transfers"]["shared_transfers"] if evidence["transfers"] else None,
                        "timing_shared": evidence["timing"]["total_shared"] if evidence["timing"] else None,
                        "timing_lead_count": evidence["timing"]["lead_count"] if evidence["timing"] else None,
                        "overlap_count": evidence["overlap"]["overlap_count"] if evidence["overlap"] else None,
                        "shared_tokens": evidence["overlap"]["shared_tokens"][:5] if evidence["overlap"] else None,
                    }.items() if v is not None
                }),
            })

        if not members:
            return None

        # Find best side wallet
        side_wallets = [m for m in members if m["is_side_wallet"]]
        best_side = None
        best_lead = 0
        for sw in side_wallets:
            if sw["avg_lead_time_seconds"] > best_lead:
                best_lead = sw["avg_lead_time_seconds"]
                best_side = sw["wallet_address"]

        # Save to database
        cluster_id = await self.db.create_cluster({
            "seed_wallet": seed_address,
            "cluster_label": f"Cluster — {len(members)} wallets",
            "total_members": len(members),
            "best_side_wallet": best_side,
            "avg_lead_time_seconds": best_lead,
        })

        for member in members:
            member["cluster_id"] = cluster_id
            await self.db.add_cluster_member(member)

            # Ensure member wallet exists in wallets table
            await self.db.upsert_wallet({
                "address": member["wallet_address"],
                "is_monitored": False,
            })

        # Promote best side wallets to monitored
        promoted = await self._promote_side_wallets(cluster_id)

        logger.info(
            "cluster_saved",
            seed=seed_address[:8],
            members=len(members),
            side_wallets=len(side_wallets),
            promoted=len(promoted),
            best_lead=f"{best_lead:.0f}s",
        )

        return {
            "seed_wallet": seed_address,
            "cluster_id": cluster_id,
            "members": members,
            "best_side_wallet": best_side,
            "promoted": promoted,
        }

    # =========================================================================
    # Step 1: Funding Source Analysis
    # =========================================================================

    async def _analyze_funding_sources(self, wallet_address: str, depth: int = 1) -> list[dict]:
        """
        Trace who funded this wallet and who it funded.
        Returns list of connected wallets with SOL transfer volumes.
        """
        transfers = await self._get_sol_transfers(wallet_address)

        # Aggregate by counterparty
        counterparties: dict[str, dict] = {}
        for t in transfers:
            if t["from_addr"] == wallet_address:
                addr = t["to_addr"]
                direction = "funded"
            elif t["to_addr"] == wallet_address:
                addr = t["from_addr"]
                direction = "funder"
            else:
                continue

            if addr not in counterparties:
                counterparties[addr] = {
                    "address": addr,
                    "direction": direction,
                    "total_sol": 0,
                    "tx_count": 0,
                    "depth": depth,
                }
            counterparties[addr]["total_sol"] += t["amount_sol"]
            counterparties[addr]["tx_count"] += 1

        results = [
            cp for cp in counterparties.values()
            if cp["total_sol"] >= self.MIN_TRANSFER_SOL
            and cp["address"] not in KNOWN_EXCHANGES
        ]

        # Go deeper if depth allows
        if depth < self.FUNDING_DEPTH and results:
            # Only trace the top 5 by volume (avoid explosion)
            top_sources = sorted(results, key=lambda x: x["total_sol"], reverse=True)[:5]
            for source in top_sources:
                await asyncio.sleep(self.BATCH_DELAY)
                deeper = await self._analyze_funding_sources(source["address"], depth + 1)
                for d in deeper:
                    if d["address"] != wallet_address and d["address"] not in counterparties:
                        d["depth"] = depth + 1
                        results.append(d)

        return results

    async def _get_sol_transfers(self, wallet_address: str) -> list[dict]:
        """Get all SOL transfers to/from a wallet using Helius parsed transactions."""
        txs = await self._fetch_transactions_batched(wallet_address)
        transfers = []

        for tx in txs:
            # Look for native SOL transfers
            native = tx.get("nativeTransfers", [])
            for nt in native:
                amount = (nt.get("amount", 0) or 0) / 1e9  # lamports to SOL
                if amount < self.MIN_TRANSFER_SOL:
                    continue
                transfers.append({
                    "from_addr": nt.get("fromUserAccount", ""),
                    "to_addr": nt.get("toUserAccount", ""),
                    "amount_sol": amount,
                    "timestamp": tx.get("timestamp", 0),
                })

        return transfers

    # =========================================================================
    # Step 2: Transfer Pattern Detection
    # =========================================================================

    async def _analyze_transfer_patterns(
        self, seed_address: str, candidate_wallets: set[str]
    ) -> list[dict]:
        """Check for SPL token transfers between seed and candidate wallets."""
        txs = await self._fetch_transactions_batched(seed_address)
        pattern_map: dict[str, dict] = {}

        for tx in txs:
            token_transfers = tx.get("tokenTransfers", [])
            for tt in token_transfers:
                from_addr = tt.get("fromUserAccount", "")
                to_addr = tt.get("toUserAccount", "")
                mint = tt.get("mint", "")

                # Check if transfer involves a candidate
                partner = None
                if from_addr == seed_address and to_addr in candidate_wallets:
                    partner = to_addr
                elif to_addr == seed_address and from_addr in candidate_wallets:
                    partner = from_addr

                if not partner:
                    continue

                if partner not in pattern_map:
                    pattern_map[partner] = {
                        "address": partner,
                        "shared_transfers": 0,
                        "tokens_transferred": set(),
                    }
                pattern_map[partner]["shared_transfers"] += 1
                pattern_map[partner]["tokens_transferred"].add(mint)

        # Convert sets to lists for JSON serialization
        results = []
        for addr, data in pattern_map.items():
            data["tokens_transferred"] = list(data["tokens_transferred"])
            results.append(data)

        return results

    # =========================================================================
    # Step 3: Timing Correlation
    # =========================================================================

    async def _analyze_timing_correlation(
        self, seed_address: str, candidate_wallets: set[str]
    ) -> list[dict]:
        """
        Check if candidate wallets consistently buy the same tokens
        BEFORE the seed wallet. This is the core "side wallet" detection.
        """
        # Get seed wallet's buy timestamps per token
        seed_buys = await self._get_wallet_buy_times(seed_address)
        if not seed_buys:
            return []

        results = []
        for candidate_addr in candidate_wallets:
            await asyncio.sleep(self.BATCH_DELAY * 0.5)  # Lighter rate limit

            candidate_buys = await self._get_wallet_buy_times(candidate_addr)
            if not candidate_buys:
                continue

            # Find overlapping tokens
            shared_tokens = set(seed_buys.keys()) & set(candidate_buys.keys())
            if len(shared_tokens) < self.MIN_TIMING_SAMPLES:
                continue

            # For each shared token, check who bought first
            lead_times = []
            lead_count = 0
            for token in shared_tokens:
                seed_time = seed_buys[token]
                candidate_time = candidate_buys[token]
                gap = (seed_time - candidate_time).total_seconds()

                if 0 < gap <= self.TIMING_WINDOW_SECONDS:
                    # Candidate bought BEFORE seed (positive gap = candidate was first)
                    lead_times.append(gap)
                    lead_count += 1

            if lead_count < 2:
                continue

            avg_lead = sum(lead_times) / len(lead_times) if lead_times else 0

            results.append({
                "address": candidate_addr,
                "shared_tokens": list(shared_tokens),
                "avg_lead_seconds": avg_lead,
                "lead_count": lead_count,
                "total_shared": len(shared_tokens),
            })

        return results

    async def _get_wallet_buy_times(self, wallet_address: str) -> dict[str, datetime]:
        """
        Get mapping of token_mint -> first buy timestamp for a wallet.
        Uses DB data first, falls back to on-chain if needed.
        """
        buy_times: dict[str, datetime] = {}

        # Try DB first (fast)
        db_trades = await self.db.get_wallet_token_trades_for_wallet(wallet_address)
        for trade in db_trades:
            mint = trade.get("token_mint")
            buy_at = trade.get("first_buy_at")
            if mint and buy_at:
                try:
                    if isinstance(buy_at, str):
                        dt = datetime.fromisoformat(buy_at.replace("Z", "+00:00"))
                    else:
                        dt = buy_at
                    buy_times[mint] = dt
                except (ValueError, TypeError):
                    pass

        # If we have enough data from DB, return it
        if len(buy_times) >= 3:
            return buy_times

        # Fall back to on-chain data
        txs = await self._fetch_transactions_batched(wallet_address, max_txs=100)
        for tx in txs:
            tx_type = tx.get("type", "")
            if tx_type != "SWAP":
                continue

            timestamp = tx.get("timestamp", 0)
            if not timestamp:
                continue

            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            fee_payer = tx.get("feePayer", "")

            # Look for tokens received (= buy)
            for tt in tx.get("tokenTransfers", []):
                if tt.get("toUserAccount") == fee_payer:
                    mint = tt.get("mint", "")
                    if mint and mint not in buy_times:
                        buy_times[mint] = dt

        return buy_times

    # =========================================================================
    # Step 4: Token Overlap
    # =========================================================================

    async def _analyze_token_overlap(
        self, seed_address: str, candidate_wallets: set[str]
    ) -> list[dict]:
        """Check if candidate wallets trade the same obscure tokens as the seed."""
        seed_tokens = set()

        # Get seed's traded tokens
        seed_trades = await self.db.get_wallet_token_trades_for_wallet(seed_address)
        for t in seed_trades:
            mint = t.get("token_mint")
            if mint:
                seed_tokens.add(mint)

        # Also from on-chain data
        txs = await self._fetch_transactions_batched(seed_address, max_txs=100)
        for tx in txs:
            for tt in tx.get("tokenTransfers", []):
                mint = tt.get("mint", "")
                if mint:
                    seed_tokens.add(mint)

        if not seed_tokens:
            return []

        results = []
        for candidate_addr in candidate_wallets:
            candidate_tokens = set()

            # DB data
            c_trades = await self.db.get_wallet_token_trades_for_wallet(candidate_addr)
            for t in c_trades:
                mint = t.get("token_mint")
                if mint:
                    candidate_tokens.add(mint)

            # On-chain data
            c_txs = await self._fetch_transactions_batched(candidate_addr, max_txs=100)
            for tx in c_txs:
                for tt in tx.get("tokenTransfers", []):
                    mint = tt.get("mint", "")
                    if mint:
                        candidate_tokens.add(mint)

            # Find overlap
            shared = seed_tokens & candidate_tokens
            # Exclude SOL and major stablecoins
            major_tokens = {
                "So11111111111111111111111111111111111111112",  # wSOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
            }
            shared -= major_tokens

            if len(shared) < self.MIN_TOKEN_OVERLAP:
                continue

            total_tokens = len(seed_tokens | candidate_tokens)
            overlap_pct = len(shared) / total_tokens if total_tokens > 0 else 0

            results.append({
                "address": candidate_addr,
                "shared_tokens": list(shared)[:10],  # Cap for storage
                "overlap_count": len(shared),
                "overlap_pct": round(overlap_pct, 3),
            })

        return results

    # =========================================================================
    # Scoring and Classification
    # =========================================================================

    def _score_relationship(self, evidence: dict) -> float:
        """
        Calculate 0.0-1.0 confidence score for a wallet relationship.

        Weights:
        - Funding link: 0.25 base
        - Token transfers: 0.20 base
        - Timing correlation: 0.35 base (strongest — this IS the side wallet signal)
        - Token overlap: 0.10 base
        - Multiple types bonus: +0.10 per additional type
        """
        score = 0.0
        types_found = 0

        if evidence.get("funding"):
            score += 0.25
            types_found += 1
            # Bonus for large funding
            if evidence["funding"].get("total_sol", 0) >= 1.0:
                score += 0.05

        if evidence.get("transfers"):
            score += 0.20
            types_found += 1
            if evidence["transfers"].get("shared_transfers", 0) >= 3:
                score += 0.05

        if evidence.get("timing"):
            td = evidence["timing"]
            score += 0.35
            types_found += 1
            # Bonus for consistent lead
            if td.get("lead_count", 0) >= 4:
                score += 0.10
            # Bonus for many shared tokens
            if td.get("total_shared", 0) >= 5:
                score += 0.05

        if evidence.get("overlap"):
            score += 0.10
            types_found += 1
            if evidence["overlap"].get("overlap_count", 0) >= 5:
                score += 0.05

        # Multi-type bonus
        if types_found >= 3:
            score += 0.10
        elif types_found >= 2:
            score += 0.05

        return min(1.0, score)

    def _classify_side_wallet(self, evidence: dict) -> bool:
        """
        Determine if this candidate is a side wallet (early accumulator).

        A side wallet:
        - Buys BEFORE the seed wallet (positive lead time)
        - Has funding connection to the seed
        - Shows timing correlation on 3+ tokens
        """
        timing = evidence.get("timing")
        if not timing:
            return False

        # Must have positive lead time (candidate buys first)
        if timing.get("avg_lead_seconds", 0) <= 0:
            return False

        # Must lead on at least 2 tokens
        if timing.get("lead_count", 0) < 2:
            return False

        # Stronger signal if also has funding link
        has_funding = evidence.get("funding") is not None
        has_overlap = evidence.get("overlap") is not None

        # If timing + at least one other evidence type → side wallet
        return has_funding or has_overlap or timing.get("lead_count", 0) >= 3

    def _primary_relationship(self, evidence: dict) -> str:
        """Determine the primary relationship type based on strongest evidence."""
        if evidence.get("timing") and evidence["timing"].get("lead_count", 0) >= 2:
            return "timing_correlated"
        if evidence.get("transfers") and evidence["transfers"].get("shared_transfers", 0) >= 2:
            return "transfer_partner"
        if evidence.get("funding"):
            return evidence["funding"].get("direction", "funding_source")
        if evidence.get("overlap"):
            return "token_overlap"
        return "funding_source"

    # =========================================================================
    # Promotion
    # =========================================================================

    async def _promote_side_wallets(self, cluster_id: int) -> list[str]:
        """
        Mark the best side wallets for monitoring.
        Returns list of newly promoted wallet addresses.
        """
        members = await self.db.get_cluster_members(cluster_id)
        side_wallets = [m for m in members if m.get("is_side_wallet")]

        # Sort by confidence then lead time
        side_wallets.sort(
            key=lambda m: (m.get("confidence", 0), m.get("avg_lead_time_seconds", 0)),
            reverse=True,
        )

        max_promote = getattr(self.settings, "max_cluster_monitored", 10)
        promoted = []

        for sw in side_wallets[:max_promote]:
            addr = sw["wallet_address"]
            await self.db.set_wallet_monitored(addr, True)
            promoted.append(addr)
            logger.info(
                "side_wallet_promoted",
                wallet=addr[:8],
                confidence=f"{sw['confidence']:.2f}",
                lead=f"{sw.get('avg_lead_time_seconds', 0):.0f}s",
            )

        return promoted

    # =========================================================================
    # Transaction Fetching (with cache)
    # =========================================================================

    async def _fetch_transactions_batched(
        self, wallet_address: str, max_txs: int = 200
    ) -> list[dict]:
        """
        Fetch parsed transactions for a wallet with caching and rate limiting.
        Uses Helius enhanced parsed transactions.
        """
        if wallet_address in self._tx_cache:
            return self._tx_cache[wallet_address]

        try:
            txs = await self.solana.get_wallet_transaction_history(
                wallet_address, max_transactions=max_txs
            )
            self._tx_cache[wallet_address] = txs or []
            await asyncio.sleep(self.BATCH_DELAY)
            return self._tx_cache[wallet_address]
        except Exception as e:
            logger.debug("tx_fetch_failed", wallet=wallet_address[:8], error=str(e))
            self._tx_cache[wallet_address] = []
            return []

    # =========================================================================
    # Reporting
    # =========================================================================

    def _print_cluster_report(self, clusters: list[dict]) -> None:
        """Print a readable summary of discovered clusters."""
        if not clusters:
            logger.info("no_clusters_found")
            return

        total_side = sum(
            1 for c in clusters
            for m in c.get("members", [])
            if m.get("is_side_wallet")
        )
        total_promoted = sum(len(c.get("promoted", [])) for c in clusters)

        logger.info(
            "cluster_report",
            clusters=len(clusters),
            total_side_wallets=total_side,
            promoted_to_monitor=total_promoted,
        )

        for c in clusters:
            seed = c["seed_wallet"][:8]
            members = c.get("members", [])
            side = [m for m in members if m.get("is_side_wallet")]
            best = c.get("best_side_wallet", "")

            logger.info(
                "cluster_detail",
                seed=seed,
                members=len(members),
                side_wallets=len(side),
                best_side=best[:8] if best else "none",
                best_lead=f"{c.get('avg_lead_time_seconds', 0):.0f}s" if best else "—",
            )
