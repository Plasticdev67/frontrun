"""
Agent Brain
============
The autonomous decision engine for Rome Agent Trader.

Unlike copy trading (which blindly follows individual wallet signals),
the Agent Brain aggregates multiple data sources and makes independent
trading decisions. It also learns from past outcomes.

Decision pipeline:
1. SCAN     — Gather signals from monitored wallets, GMGN, FOMO traders
2. AGGREGATE — Count how many smart wallets are buying the same token
3. SCORE    — Weight by wallet quality, token fundamentals, timing
4. DECIDE   — Buy/sell based on confidence threshold + risk checks
5. LOG      — Record every decision for review and learning
6. LEARN    — Periodically analyze the journal and adjust strategy

Strategy Parameters (learnable):
- min_confidence: minimum confidence to execute a trade (starts 0.6)
- wallet_trust: per-wallet multiplier based on their signal accuracy
- consensus_threshold: how many wallets must buy before we follow
- position_scale: multiplier for position sizing (0.5x-2x base size)

The agent saves its learned strategy to data/agent_strategy.json
so it persists across restarts and improves over time.
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# Default strategy — the agent starts here and adjusts based on outcomes
DEFAULT_STRATEGY = {
    "version": 1,
    "min_confidence": 0.6,
    "consensus_threshold": 2,         # Min wallets buying same token to trigger
    "position_scale": 1.0,            # Multiplier on base position size
    "max_concurrent_decisions": 5,     # Max pending buy decisions at once
    "cooldown_seconds": 300,           # Min time between buys on same token
    "wallet_trust": {},                # address → trust multiplier (0.1 - 3.0)
    "token_blacklist": [],             # Tokens we've learned to avoid
    "preferred_mcap_range": [50_000, 10_000_000],  # Learned sweet spot
    "preferred_liquidity_min": 10_000,
    "stats": {
        "total_decisions": 0,
        "total_buys": 0,
        "total_skips": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_sol": 0.0,
        "best_trade_sol": 0.0,
        "worst_trade_sol": 0.0,
        "learning_cycles": 0,
    },
}


class AgentBrain:
    """
    The autonomous trading agent for Rome Agent Trader.

    Aggregates signals, makes decisions, and learns from outcomes.
    """

    def __init__(self, settings, db, strategy_path: str | None = None):
        self.settings = settings
        self.db = db
        self.strategy_path = strategy_path or str(
            Path(settings.db_path).parent / "agent_strategy.json"
        )
        self.strategy = self._load_strategy()
        self._recent_decisions: dict[str, float] = {}  # token_mint → timestamp (cooldown)

    def _load_strategy(self) -> dict:
        """Load the agent's learned strategy from disk, or use defaults."""
        path = Path(self.strategy_path)
        if path.exists():
            try:
                with open(path) as f:
                    strategy = json.load(f)
                logger.info(
                    "agent_strategy_loaded",
                    version=strategy.get("version", 0),
                    decisions=strategy.get("stats", {}).get("total_decisions", 0),
                    wins=strategy.get("stats", {}).get("wins", 0),
                )
                # Merge with defaults to pick up new fields
                merged = {**DEFAULT_STRATEGY, **strategy}
                merged["stats"] = {**DEFAULT_STRATEGY["stats"], **strategy.get("stats", {})}
                return merged
            except Exception as e:
                logger.warning("agent_strategy_load_failed", error=str(e))
        logger.info("agent_strategy_using_defaults")
        return json.loads(json.dumps(DEFAULT_STRATEGY))

    def _save_strategy(self) -> None:
        """Persist the agent's learned strategy to disk."""
        path = Path(self.strategy_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.strategy, f, indent=2)
        logger.debug("agent_strategy_saved")

    # =========================================================================
    # SCAN — Gather current signals
    # =========================================================================

    async def scan_signals(self) -> list[dict]:
        """
        Scan all data sources for actionable signals.

        Returns a list of raw signal dicts, each with:
        - token_mint, token_symbol
        - wallet_address, wallet_score
        - signal_type: "buy" or "sell"
        - source: "monitor", "gmgn", "fomo"
        - timestamp
        """
        signals = []

        # Source 1: Recent signals from wallet monitor (last 30 min)
        try:
            rows = await self.db.connection.execute(
                """SELECT s.wallet_address, s.token_mint, s.token_symbol,
                          s.signal_type, s.confidence, s.created_at,
                          w.total_score, w.gmgn_profit_30d_usd, w.gmgn_winrate, w.source
                   FROM signals s
                   LEFT JOIN wallets w ON s.wallet_address = w.address
                   WHERE s.created_at >= datetime('now', '-30 minutes')
                   ORDER BY s.created_at DESC"""
            )
            for r in await rows.fetchall():
                d = dict(r)
                signals.append({
                    "token_mint": d["token_mint"],
                    "token_symbol": d.get("token_symbol"),
                    "wallet_address": d["wallet_address"],
                    "wallet_score": d.get("total_score") or 0,
                    "wallet_profit_30d": d.get("gmgn_profit_30d_usd") or 0,
                    "wallet_winrate": d.get("gmgn_winrate"),
                    "wallet_source": d.get("source", "unknown"),
                    "signal_type": d["signal_type"],
                    "confidence": d.get("confidence") or 0.5,
                    "source": "monitor",
                    "timestamp": d["created_at"],
                })
        except Exception as e:
            logger.warning("agent_scan_signals_error", error=str(e))

        # Source 2: FOMO trader activity (from fomo_traders table)
        try:
            rows = await self.db.connection.execute(
                """SELECT ft.wallet_address, ft.username, ft.ranking, ft.pnl_24h_usd,
                          w.total_score
                   FROM fomo_traders ft
                   LEFT JOIN wallets w ON ft.wallet_address = w.address
                   WHERE ft.is_tracked = TRUE"""
            )
            fomo_wallets = [dict(r) for r in await rows.fetchall()]
            if fomo_wallets:
                logger.debug("agent_fomo_wallets_loaded", count=len(fomo_wallets))
                # FOMO wallets are passively tracked — their signals come
                # through the monitor if they're also in the wallets table
        except Exception:
            pass  # Table might not exist yet

        logger.info("agent_scan_complete", signal_count=len(signals))
        return signals

    # =========================================================================
    # AGGREGATE — Group signals by token
    # =========================================================================

    def aggregate_signals(self, signals: list[dict]) -> list[dict]:
        """
        Group signals by token and calculate consensus metrics.

        Returns a list of token opportunities, each with:
        - token_mint, token_symbol
        - buy_count: how many wallets are buying
        - sell_count: how many wallets are selling
        - avg_wallet_score: average score of buying wallets
        - top_wallet_profit: best wallet's 30D profit
        - wallets: list of wallet addresses buying
        - raw_confidence: aggregated confidence score
        """
        from collections import defaultdict

        by_token: dict[str, dict] = defaultdict(lambda: {
            "buy_signals": [],
            "sell_signals": [],
        })

        for sig in signals:
            mint = sig["token_mint"]
            if sig["signal_type"] in ("buy", "large_buy"):
                by_token[mint]["buy_signals"].append(sig)
            elif sig["signal_type"] in ("sell", "large_sell"):
                by_token[mint]["sell_signals"].append(sig)

        opportunities = []
        for mint, data in by_token.items():
            buys = data["buy_signals"]
            sells = data["sell_signals"]

            if not buys:
                continue

            # Use the first buy signal for token symbol
            token_symbol = buys[0].get("token_symbol") or mint[:8]

            # Calculate aggregate metrics
            wallet_scores = [b["wallet_score"] for b in buys]
            wallet_profits = [b.get("wallet_profit_30d") or 0 for b in buys]
            wallet_addresses = list(set(b["wallet_address"] for b in buys))

            # Apply wallet trust multipliers
            trust_adjusted_scores = []
            for b in buys:
                addr = b["wallet_address"]
                trust = self.strategy.get("wallet_trust", {}).get(addr, 1.0)
                trust_adjusted_scores.append(b["wallet_score"] * trust)

            avg_score = sum(trust_adjusted_scores) / len(trust_adjusted_scores) if trust_adjusted_scores else 0
            top_profit = max(wallet_profits) if wallet_profits else 0

            # Raw confidence: combines consensus count, wallet quality, and individual confidences
            consensus_factor = min(len(buys) / max(self.strategy["consensus_threshold"], 1), 2.0)
            quality_factor = min(avg_score / 70, 1.5)  # 70-score wallet = 1.0x
            individual_conf = sum(b.get("confidence", 0.5) for b in buys) / len(buys)

            raw_confidence = round(
                individual_conf * 0.3 + consensus_factor * 0.4 + quality_factor * 0.3,
                3,
            )

            opportunities.append({
                "token_mint": mint,
                "token_symbol": token_symbol,
                "buy_count": len(buys),
                "sell_count": len(sells),
                "avg_wallet_score": round(avg_score, 1),
                "top_wallet_profit": top_profit,
                "wallets": wallet_addresses,
                "raw_confidence": raw_confidence,
            })

        # Sort by confidence descending
        opportunities.sort(key=lambda x: x["raw_confidence"], reverse=True)
        return opportunities

    # =========================================================================
    # DECIDE — Make buy/sell/skip decisions
    # =========================================================================

    async def make_decisions(self, opportunities: list[dict]) -> list[dict]:
        """
        Evaluate each opportunity and decide whether to act.

        For each token opportunity, the agent:
        1. Checks if we already hold this token
        2. Checks cooldown (don't re-buy too quickly)
        3. Checks risk limits (max positions, daily loss)
        4. Applies confidence threshold
        5. Determines position size based on confidence
        6. Logs the decision

        Returns a list of decision dicts ready for execution.
        """
        decisions = []
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()

        # Get current state
        open_positions = await self.db.get_open_positions()
        open_mints = {p["token_mint"] for p in open_positions}
        daily_pnl = await self.db.get_todays_pnl()
        position_count = len(open_positions)

        for opp in opportunities:
            mint = opp["token_mint"]
            symbol = opp["token_symbol"]
            confidence = opp["raw_confidence"]
            reasons = []

            # --- Pre-flight checks ---

            # Check 1: Already holding?
            if mint in open_mints:
                reasons.append("already_holding")
                await self._log_decision(opp, "hold", confidence, reasons)
                continue

            # Check 2: Cooldown
            last_decision_ts = self._recent_decisions.get(mint, 0)
            if now_ts - last_decision_ts < self.strategy["cooldown_seconds"]:
                reasons.append("cooldown_active")
                await self._log_decision(opp, "skip", confidence, reasons)
                continue

            # Check 3: Max positions
            if position_count >= self.settings.max_open_positions:
                reasons.append("max_positions_reached")
                await self._log_decision(opp, "skip", confidence, reasons)
                continue

            # Check 4: Daily loss limit
            if daily_pnl <= -self.settings.max_daily_loss_sol:
                reasons.append("daily_loss_limit")
                await self._log_decision(opp, "skip", confidence, reasons)
                continue

            # Check 5: Token blacklist
            if mint in self.strategy.get("token_blacklist", []):
                reasons.append("blacklisted_token")
                await self._log_decision(opp, "skip", confidence, reasons)
                continue

            # Check 6: Confidence threshold
            min_conf = self.strategy["min_confidence"]
            if confidence < min_conf:
                reasons.append(f"low_confidence_{confidence:.2f}<{min_conf:.2f}")
                await self._log_decision(opp, "skip", confidence, reasons)
                continue

            # --- DECISION: BUY ---

            # Calculate position size based on confidence
            base_size = self.settings.default_position_size_sol
            scale = self.strategy["position_scale"]

            # Higher confidence → bigger position (up to 2x base)
            confidence_multiplier = 0.5 + (confidence * 1.0)  # 0.5x at 0, 1.5x at 1.0
            amount_sol = round(base_size * scale * confidence_multiplier, 4)

            # Cap at max position size
            amount_sol = min(amount_sol, self.settings.max_position_size_sol)

            reasons.append(f"consensus_{opp['buy_count']}_wallets")
            reasons.append(f"avg_score_{opp['avg_wallet_score']:.0f}")
            if opp["top_wallet_profit"] > 10000:
                reasons.append(f"top_wallet_${opp['top_wallet_profit']:,.0f}_profit")

            decision = {
                "token_mint": mint,
                "token_symbol": symbol,
                "decision": "buy",
                "confidence": confidence,
                "reasons": reasons,
                "amount_sol": amount_sol,
                "wallets_buying": opp["buy_count"],
                "wallets_selling": opp["sell_count"],
                "avg_wallet_score": opp["avg_wallet_score"],
                "wallets": opp["wallets"],
            }

            decisions.append(decision)
            self._recent_decisions[mint] = now_ts
            position_count += 1  # Track for max positions check

            await self._log_decision(opp, "buy", confidence, reasons, amount_sol)

            # Limit concurrent decisions per cycle
            if len(decisions) >= self.strategy["max_concurrent_decisions"]:
                break

        # Update stats
        self.strategy["stats"]["total_decisions"] += len(opportunities)
        self.strategy["stats"]["total_buys"] += len(decisions)
        self.strategy["stats"]["total_skips"] += len(opportunities) - len(decisions)
        self._save_strategy()

        logger.info(
            "agent_decisions_made",
            opportunities=len(opportunities),
            buys=len(decisions),
            skips=len(opportunities) - len(decisions),
        )

        return decisions

    async def _log_decision(
        self,
        opportunity: dict,
        decision: str,
        confidence: float,
        reasons: list[str],
        amount_sol: float = 0,
    ) -> None:
        """Log a decision to the agent_decisions table."""
        try:
            await self.db.insert_agent_decision({
                "token_mint": opportunity["token_mint"],
                "token_symbol": opportunity.get("token_symbol"),
                "decision": decision,
                "confidence": confidence,
                "reasons": reasons,
                "wallets_buying": opportunity.get("buy_count", 0),
                "wallets_selling": opportunity.get("sell_count", 0),
                "avg_wallet_score": opportunity.get("avg_wallet_score", 0),
                "amount_sol": amount_sol,
            })
        except Exception as e:
            logger.warning("agent_log_decision_error", error=str(e))

    # =========================================================================
    # LEARN — Analyze journal and adjust strategy
    # =========================================================================

    async def learn_from_journal(self) -> dict:
        """
        Analyze past agent decisions and their outcomes to improve strategy.

        This is the core learning loop. It:
        1. Queries all closed agent decisions with outcomes
        2. Calculates performance by confidence level
        3. Calculates performance by wallet source
        4. Adjusts strategy parameters
        5. Saves the updated strategy

        Returns a summary of what was learned.
        """
        logger.info("agent_learning_started")
        insights = {
            "decisions_analyzed": 0,
            "adjustments": [],
        }

        try:
            # Get all agent decisions that have outcomes
            rows = await self.db.connection.execute(
                """SELECT ad.*, p.realized_pnl_sol, p.status as position_status
                   FROM agent_decisions ad
                   LEFT JOIN trades t ON ad.trade_id = t.id
                   LEFT JOIN positions p ON p.token_mint = ad.token_mint AND p.status = 'closed'
                   WHERE ad.decision = 'buy' AND ad.executed = TRUE"""
            )
            decisions = [dict(r) for r in await rows.fetchall()]
            insights["decisions_analyzed"] = len(decisions)

            if len(decisions) < 5:
                logger.info("agent_learning_skipped", reason="not_enough_data", count=len(decisions))
                insights["adjustments"].append("Not enough data yet (need 5+ closed trades)")
                return insights

            # --- Analysis 1: Performance by confidence bucket ---
            conf_buckets = {"low": [], "mid": [], "high": []}
            for d in decisions:
                pnl = d.get("realized_pnl_sol") or 0
                conf = d.get("confidence") or 0

                if conf < 0.5:
                    conf_buckets["low"].append(pnl)
                elif conf < 0.75:
                    conf_buckets["mid"].append(pnl)
                else:
                    conf_buckets["high"].append(pnl)

            for bucket, pnls in conf_buckets.items():
                if pnls:
                    avg_pnl = sum(pnls) / len(pnls)
                    win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
                    logger.info(
                        "agent_confidence_analysis",
                        bucket=bucket,
                        trades=len(pnls),
                        avg_pnl=f"{avg_pnl:.4f}",
                        win_rate=f"{win_rate:.0%}",
                    )

            # Adjust min_confidence: if low-confidence trades lose money, raise threshold
            low_pnls = conf_buckets["low"]
            if len(low_pnls) >= 3:
                low_avg = sum(low_pnls) / len(low_pnls)
                if low_avg < 0:
                    old_conf = self.strategy["min_confidence"]
                    self.strategy["min_confidence"] = min(old_conf + 0.05, 0.85)
                    insights["adjustments"].append(
                        f"Raised min_confidence {old_conf:.2f} -> {self.strategy['min_confidence']:.2f} "
                        f"(low-conf trades avg PnL: {low_avg:.4f} SOL)"
                    )
                elif low_avg > 0:
                    old_conf = self.strategy["min_confidence"]
                    self.strategy["min_confidence"] = max(old_conf - 0.03, 0.4)
                    insights["adjustments"].append(
                        f"Lowered min_confidence {old_conf:.2f} -> {self.strategy['min_confidence']:.2f} "
                        f"(low-conf trades profitable)"
                    )

            # --- Analysis 2: Performance by triggering wallet ---
            wallet_perf: dict[str, list[float]] = {}
            for d in decisions:
                pnl = d.get("realized_pnl_sol") or 0
                # Get wallets that triggered this decision
                reasons_raw = d.get("reasons") or "[]"
                try:
                    reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
                except (json.JSONDecodeError, TypeError):
                    reasons = []

                # We logged wallet addresses in the decision's wallets_buying context
                # For now, use the wallet_address from the trade if available
                wallet = d.get("wallet_address")
                if wallet:
                    if wallet not in wallet_perf:
                        wallet_perf[wallet] = []
                    wallet_perf[wallet].append(pnl)

            # Update wallet trust scores
            for addr, pnls in wallet_perf.items():
                if len(pnls) >= 2:
                    win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
                    avg_pnl = sum(pnls) / len(pnls)

                    # Trust formula: base 1.0, +/- based on performance
                    current_trust = self.strategy.get("wallet_trust", {}).get(addr, 1.0)
                    if win_rate >= 0.6 and avg_pnl > 0:
                        new_trust = min(current_trust + 0.2, 3.0)
                    elif win_rate < 0.3 or avg_pnl < -0.01:
                        new_trust = max(current_trust - 0.3, 0.1)
                    else:
                        new_trust = current_trust

                    if new_trust != current_trust:
                        self.strategy.setdefault("wallet_trust", {})[addr] = round(new_trust, 2)
                        insights["adjustments"].append(
                            f"Wallet {addr[:8]} trust: {current_trust:.1f} -> {new_trust:.1f} "
                            f"(WR: {win_rate:.0%}, avg PnL: {avg_pnl:.4f})"
                        )

            # --- Analysis 3: Overall performance ---
            all_pnls = [d.get("realized_pnl_sol") or 0 for d in decisions if d.get("realized_pnl_sol") is not None]
            if all_pnls:
                total_pnl = sum(all_pnls)
                overall_wr = sum(1 for p in all_pnls if p > 0) / len(all_pnls)
                best = max(all_pnls)
                worst = min(all_pnls)

                self.strategy["stats"]["wins"] = sum(1 for p in all_pnls if p > 0)
                self.strategy["stats"]["losses"] = sum(1 for p in all_pnls if p <= 0)
                self.strategy["stats"]["total_pnl_sol"] = round(total_pnl, 6)
                self.strategy["stats"]["best_trade_sol"] = round(best, 6)
                self.strategy["stats"]["worst_trade_sol"] = round(worst, 6)

                # Adjust position scale based on overall performance
                if overall_wr >= 0.55 and total_pnl > 0:
                    old_scale = self.strategy["position_scale"]
                    self.strategy["position_scale"] = min(old_scale + 0.1, 2.5)
                    if self.strategy["position_scale"] != old_scale:
                        insights["adjustments"].append(
                            f"Increased position_scale {old_scale:.1f} -> {self.strategy['position_scale']:.1f} "
                            f"(WR: {overall_wr:.0%}, total PnL: {total_pnl:.4f})"
                        )
                elif overall_wr < 0.4 or total_pnl < 0:
                    old_scale = self.strategy["position_scale"]
                    self.strategy["position_scale"] = max(old_scale - 0.15, 0.3)
                    if self.strategy["position_scale"] != old_scale:
                        insights["adjustments"].append(
                            f"Decreased position_scale {old_scale:.1f} -> {self.strategy['position_scale']:.1f} "
                            f"(WR: {overall_wr:.0%}, total PnL: {total_pnl:.4f})"
                        )

            # --- Analysis 4: Token blacklist learning ---
            # If we've lost on a token multiple times, blacklist it
            token_pnl: dict[str, list[float]] = {}
            for d in decisions:
                mint = d["token_mint"]
                pnl = d.get("realized_pnl_sol") or 0
                if mint not in token_pnl:
                    token_pnl[mint] = []
                token_pnl[mint].append(pnl)

            for mint, pnls in token_pnl.items():
                if len(pnls) >= 2 and all(p < 0 for p in pnls):
                    if mint not in self.strategy.get("token_blacklist", []):
                        self.strategy.setdefault("token_blacklist", []).append(mint)
                        insights["adjustments"].append(
                            f"Blacklisted token {mint[:8]} (lost on {len(pnls)} trades)"
                        )

            # Save everything
            self.strategy["stats"]["learning_cycles"] += 1
            self._save_strategy()

            logger.info(
                "agent_learning_complete",
                analyzed=len(decisions),
                adjustments=len(insights["adjustments"]),
                cycle=self.strategy["stats"]["learning_cycles"],
            )

        except Exception as e:
            logger.error("agent_learning_error", error=str(e))
            insights["adjustments"].append(f"Error: {str(e)}")

        return insights

    # =========================================================================
    # RUN — Full decision cycle
    # =========================================================================

    async def run_cycle(self) -> list[dict]:
        """
        Run one full agent decision cycle.

        Returns list of buy decisions ready for execution by the trade executor.
        """
        logger.info("agent_cycle_start")

        # 1. Scan for signals
        signals = await self.scan_signals()
        if not signals:
            logger.info("agent_cycle_no_signals")
            return []

        # 2. Aggregate by token
        opportunities = self.aggregate_signals(signals)
        if not opportunities:
            logger.info("agent_cycle_no_opportunities")
            return []

        # 3. Make decisions
        decisions = await self.make_decisions(opportunities)

        logger.info(
            "agent_cycle_complete",
            signals=len(signals),
            opportunities=len(opportunities),
            decisions=len(decisions),
        )

        return decisions

    def get_strategy_summary(self) -> dict:
        """Get a human-readable summary of the agent's current strategy."""
        s = self.strategy
        stats = s.get("stats", {})
        total = stats.get("wins", 0) + stats.get("losses", 0)
        win_rate = stats["wins"] / total * 100 if total > 0 else 0

        trusted_wallets = {
            addr: trust for addr, trust in s.get("wallet_trust", {}).items()
            if trust != 1.0
        }

        return {
            "min_confidence": s["min_confidence"],
            "consensus_threshold": s["consensus_threshold"],
            "position_scale": s["position_scale"],
            "total_decisions": stats.get("total_decisions", 0),
            "win_rate": round(win_rate, 1),
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
            "total_pnl_sol": stats.get("total_pnl_sol", 0),
            "best_trade_sol": stats.get("best_trade_sol", 0),
            "worst_trade_sol": stats.get("worst_trade_sol", 0),
            "learning_cycles": stats.get("learning_cycles", 0),
            "trusted_wallets_adjusted": len(trusted_wallets),
            "blacklisted_tokens": len(s.get("token_blacklist", [])),
        }
