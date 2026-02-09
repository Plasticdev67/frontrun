"""
Database Manager
================
Handles all database operations: creating tables, inserting data, querying.

Uses SQLite because:
- No server to manage (it's just a file)
- Fast enough for our use case (we're not doing millions of queries/sec)
- Easy to backup (just copy the .db file)
- We use async (aiosqlite) so database operations don't block the bot

Every function here is a clean interface to the database.
Other modules never write raw SQL — they call these functions instead.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from database.models import CREATE_TABLES_SQL
from utils.logger import get_logger

logger = get_logger(__name__)


class Database:
    """
    Async database manager for Rome Agent Trader.

    Usage:
        db = Database("path/to/database.db")
        await db.initialize()  # Creates tables if they don't exist
        await db.insert_token({...})
        await db.close()
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """
        Connect to the database and create tables if they don't exist.
        Called once when the bot starts up.
        """
        # Make sure the directory for the database file exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.connection = await aiosqlite.connect(self.db_path)
        # Enable WAL mode for better concurrent read/write performance
        await self.connection.execute("PRAGMA journal_mode=WAL")
        # Return rows as dictionaries instead of tuples (much easier to work with)
        self.connection.row_factory = aiosqlite.Row

        # Create all tables (IF NOT EXISTS means it's safe to run multiple times)
        await self.connection.executescript(CREATE_TABLES_SQL)
        await self.connection.commit()

        # Migrate existing databases — add new columns if they don't exist yet
        await self._run_migrations()

        logger.info("database_initialized", path=self.db_path)

    async def _run_migrations(self) -> None:
        """Add new columns to existing tables. Safe to run multiple times."""
        migrations = [
            ("wallets", "win_rate", "REAL DEFAULT 0"),
            ("wallets", "gmgn_realized_profit_usd", "REAL DEFAULT 0"),
            ("wallets", "gmgn_profit_30d_usd", "REAL DEFAULT 0"),
            ("wallets", "gmgn_sol_balance", "REAL DEFAULT 0"),
            ("wallets", "gmgn_winrate", "REAL"),
            ("wallets", "gmgn_buy_30d", "INTEGER DEFAULT 0"),
            ("wallets", "gmgn_sell_30d", "INTEGER DEFAULT 0"),
            ("wallets", "gmgn_tags", "TEXT"),
        ]
        for table, column, col_type in migrations:
            try:
                await self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
            except Exception:
                pass  # Column already exists — that's fine
        await self.connection.commit()

    async def close(self) -> None:
        """Close the database connection cleanly."""
        if self.connection:
            await self.connection.close()
            logger.info("database_closed")

    # =========================================================================
    # Token Operations (Stage 1: Discovery)
    # =========================================================================

    async def insert_token(self, token_data: dict[str, Any]) -> int:
        """
        Save a discovered token to the database.
        Returns the token's database ID.

        If the token already exists (same mint address), it updates the record
        instead of creating a duplicate.
        """
        sql = """
            INSERT INTO tokens (
                mint_address, symbol, name, market_cap_usd, price_usd,
                price_change_pct, price_multiplier, volume_24h_usd,
                liquidity_usd, holder_count, pair_address, dex_name, data_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint_address) DO UPDATE SET
                market_cap_usd = excluded.market_cap_usd,
                price_usd = excluded.price_usd,
                price_change_pct = excluded.price_change_pct,
                price_multiplier = excluded.price_multiplier,
                volume_24h_usd = excluded.volume_24h_usd,
                liquidity_usd = excluded.liquidity_usd,
                holder_count = excluded.holder_count
        """
        cursor = await self.connection.execute(sql, (
            token_data.get("mint_address"),
            token_data.get("symbol"),
            token_data.get("name"),
            token_data.get("market_cap_usd"),
            token_data.get("price_usd"),
            token_data.get("price_change_pct"),
            token_data.get("price_multiplier"),
            token_data.get("volume_24h_usd"),
            token_data.get("liquidity_usd"),
            token_data.get("holder_count"),
            token_data.get("pair_address"),
            token_data.get("dex_name"),
            token_data.get("data_source"),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def get_top_tokens(self, limit: int = 50) -> list[dict]:
        """Get the top-performing discovered tokens, sorted by price multiplier."""
        sql = """
            SELECT * FROM tokens
            ORDER BY price_multiplier DESC
            LIMIT ?
        """
        cursor = await self.connection.execute(sql, (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_token_by_mint(self, mint_address: str) -> dict | None:
        """Look up a specific token by its mint address."""
        sql = "SELECT * FROM tokens WHERE mint_address = ?"
        cursor = await self.connection.execute(sql, (mint_address,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    # =========================================================================
    # Wallet Operations (Stage 2: Analyzer)
    # =========================================================================

    async def upsert_wallet(self, wallet_data: dict[str, Any]) -> int:
        """
        Insert or update a wallet record.
        'Upsert' means: insert if new, update if it already exists.
        Stores both scoring data and GMGN enrichment data.
        """
        import json

        sql = """
            INSERT INTO wallets (
                address, total_score, pnl_score, win_rate_score, timing_score,
                consistency_score, total_pnl_sol, total_trades, winning_trades,
                win_rate, avg_entry_rank, unique_winners,
                gmgn_realized_profit_usd, gmgn_profit_30d_usd, gmgn_sol_balance,
                gmgn_winrate, gmgn_buy_30d, gmgn_sell_30d, gmgn_tags,
                is_flagged, flag_reason, is_monitored, last_active, score_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                total_score = excluded.total_score,
                pnl_score = excluded.pnl_score,
                win_rate_score = excluded.win_rate_score,
                timing_score = excluded.timing_score,
                consistency_score = excluded.consistency_score,
                total_pnl_sol = excluded.total_pnl_sol,
                total_trades = excluded.total_trades,
                winning_trades = excluded.winning_trades,
                win_rate = excluded.win_rate,
                avg_entry_rank = excluded.avg_entry_rank,
                unique_winners = excluded.unique_winners,
                gmgn_realized_profit_usd = excluded.gmgn_realized_profit_usd,
                gmgn_profit_30d_usd = excluded.gmgn_profit_30d_usd,
                gmgn_sol_balance = excluded.gmgn_sol_balance,
                gmgn_winrate = excluded.gmgn_winrate,
                gmgn_buy_30d = excluded.gmgn_buy_30d,
                gmgn_sell_30d = excluded.gmgn_sell_30d,
                gmgn_tags = excluded.gmgn_tags,
                is_flagged = excluded.is_flagged,
                flag_reason = excluded.flag_reason,
                is_monitored = excluded.is_monitored,
                last_active = excluded.last_active,
                score_updated_at = excluded.score_updated_at
        """
        now = datetime.now(timezone.utc).isoformat()

        # Serialize tags list to JSON string for storage
        gmgn_tags = wallet_data.get("gmgn_tags") or []
        tags_json = json.dumps(gmgn_tags) if isinstance(gmgn_tags, list) else "[]"

        cursor = await self.connection.execute(sql, (
            wallet_data["address"],
            wallet_data.get("total_score", 0),
            wallet_data.get("pnl_score", 0),
            wallet_data.get("win_rate_score", 0),
            wallet_data.get("timing_score", 0),
            wallet_data.get("consistency_score", 0),
            wallet_data.get("total_pnl_sol", 0),
            wallet_data.get("total_trades", 0),
            wallet_data.get("winning_trades", 0),
            wallet_data.get("win_rate", 0),
            wallet_data.get("avg_entry_rank", 0),
            wallet_data.get("unique_winners", 0),
            wallet_data.get("gmgn_realized_profit_usd", 0),
            wallet_data.get("gmgn_profit_30d_usd", 0),
            wallet_data.get("gmgn_sol_balance", 0),
            wallet_data.get("gmgn_winrate"),
            wallet_data.get("gmgn_buy_30d", 0),
            wallet_data.get("gmgn_sell_30d", 0),
            tags_json,
            wallet_data.get("is_flagged", False),
            wallet_data.get("flag_reason"),
            wallet_data.get("is_monitored", False),
            wallet_data.get("last_active", now),
            now,
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def get_top_wallets(self, limit: int = 50, only_monitored: bool = False) -> list[dict]:
        """Get top-scored wallets. Optionally filter to only monitored ones."""
        if only_monitored:
            sql = "SELECT * FROM wallets WHERE is_monitored = TRUE ORDER BY total_score DESC LIMIT ?"
        else:
            sql = "SELECT * FROM wallets WHERE is_flagged = FALSE ORDER BY total_score DESC LIMIT ?"
        cursor = await self.connection.execute(sql, (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_monitored_wallets(self) -> list[dict]:
        """Get all wallets that are being actively monitored."""
        sql = "SELECT * FROM wallets WHERE is_monitored = TRUE ORDER BY total_score DESC"
        cursor = await self.connection.execute(sql)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_wallet_monitored(self, address: str, monitored: bool) -> None:
        """Turn monitoring on or off for a specific wallet."""
        sql = "UPDATE wallets SET is_monitored = ? WHERE address = ?"
        await self.connection.execute(sql, (monitored, address))
        await self.connection.commit()

    # =========================================================================
    # Wallet-Token Trade Links (Stage 2: Analyzer)
    # =========================================================================

    async def insert_wallet_token_trade(self, trade_data: dict[str, Any]) -> int:
        """Record that a wallet traded a specific token (used for scoring)."""
        sql = """
            INSERT INTO wallet_token_trades (
                wallet_address, token_mint, token_symbol, buy_amount_sol,
                sell_amount_sol, pnl_sol, buy_price, sell_price, entry_rank,
                first_buy_at, last_sell_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.connection.execute(sql, (
            trade_data["wallet_address"],
            trade_data["token_mint"],
            trade_data.get("token_symbol"),
            trade_data.get("buy_amount_sol"),
            trade_data.get("sell_amount_sol"),
            trade_data.get("pnl_sol"),
            trade_data.get("buy_price"),
            trade_data.get("sell_price"),
            trade_data.get("entry_rank"),
            trade_data.get("first_buy_at"),
            trade_data.get("last_sell_at"),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    # =========================================================================
    # Signal Operations (Stage 3: Monitor)
    # =========================================================================

    async def insert_signal(self, signal_data: dict[str, Any]) -> int:
        """Record a new copy trading signal."""
        sql = """
            INSERT INTO signals (
                wallet_address, token_mint, token_symbol, signal_type,
                wallet_score, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        cursor = await self.connection.execute(sql, (
            signal_data["wallet_address"],
            signal_data["token_mint"],
            signal_data.get("token_symbol"),
            signal_data["signal_type"],
            signal_data.get("wallet_score"),
            signal_data.get("confidence"),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def mark_signal_executed(self, signal_id: int, trade_id: int) -> None:
        """Mark a signal as having been executed, linking it to the trade."""
        sql = "UPDATE signals SET executed = TRUE, trade_id = ? WHERE id = ?"
        await self.connection.execute(sql, (trade_id, signal_id))
        await self.connection.commit()

    async def mark_signal_skipped(self, signal_id: int, reason: str) -> None:
        """Mark a signal as skipped, recording why."""
        sql = "UPDATE signals SET skip_reason = ? WHERE id = ?"
        await self.connection.execute(sql, (reason, signal_id))
        await self.connection.commit()

    # =========================================================================
    # Trade Operations (Stage 4: Executor)
    # =========================================================================

    async def insert_trade(self, trade_data: dict[str, Any]) -> int:
        """
        Record a trade the bot executed. This is the audit trail.
        Every trade — successful or failed — gets logged here.
        """
        sql = """
            INSERT INTO trades (
                token_mint, token_symbol, side, amount_sol, amount_tokens,
                price_usd, triggered_by_wallet, signal_id, sell_reason,
                tx_signature, status, slippage_actual_bps,
                priority_fee_lamports, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.connection.execute(sql, (
            trade_data["token_mint"],
            trade_data.get("token_symbol"),
            trade_data["side"],
            trade_data["amount_sol"],
            trade_data.get("amount_tokens"),
            trade_data.get("price_usd"),
            trade_data.get("triggered_by_wallet"),
            trade_data.get("signal_id"),
            trade_data.get("sell_reason"),
            trade_data.get("tx_signature"),
            trade_data.get("status", "pending"),
            trade_data.get("slippage_actual_bps"),
            trade_data.get("priority_fee_lamports"),
            trade_data.get("error_message"),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def update_trade_status(
        self, trade_id: int, status: str, tx_signature: str | None = None, error: str | None = None
    ) -> None:
        """Update a trade's status after it confirms or fails on-chain."""
        now = datetime.now(timezone.utc).isoformat()
        if status == "confirmed":
            sql = "UPDATE trades SET status = ?, tx_signature = COALESCE(?, tx_signature), confirmed_at = ? WHERE id = ?"
            await self.connection.execute(sql, (status, tx_signature, now, trade_id))
        else:
            sql = "UPDATE trades SET status = ?, error_message = ? WHERE id = ?"
            await self.connection.execute(sql, (status, error, trade_id))
        await self.connection.commit()

    async def get_todays_trades(self) -> list[dict]:
        """Get all trades executed today (for daily loss tracking)."""
        sql = "SELECT * FROM trades WHERE date(created_at) = date('now') ORDER BY created_at DESC"
        cursor = await self.connection.execute(sql)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_todays_pnl(self) -> float:
        """Calculate total PnL for today (used for daily loss limit check)."""
        sql = """
            SELECT COALESCE(SUM(
                CASE WHEN side = 'sell' THEN amount_sol
                     WHEN side = 'buy' THEN -amount_sol
                     ELSE 0 END
            ), 0) as daily_pnl
            FROM trades
            WHERE date(created_at) = date('now') AND status = 'confirmed'
        """
        cursor = await self.connection.execute(sql)
        row = await cursor.fetchone()
        return float(row["daily_pnl"]) if row else 0.0

    # =========================================================================
    # Position Operations (Stage 4: Executor)
    # =========================================================================

    async def open_position(self, position_data: dict[str, Any]) -> int:
        """Open a new trading position."""
        tp_levels_json = json.dumps(position_data.get("take_profit_levels", []))
        sql = """
            INSERT INTO positions (
                token_mint, token_symbol, entry_price_usd, amount_sol_invested,
                amount_tokens_held, take_profit_levels, stop_loss_price,
                triggered_by_wallet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.connection.execute(sql, (
            position_data["token_mint"],
            position_data.get("token_symbol"),
            position_data["entry_price_usd"],
            position_data["amount_sol_invested"],
            position_data["amount_tokens_held"],
            tp_levels_json,
            position_data.get("stop_loss_price"),
            position_data.get("triggered_by_wallet"),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def get_open_positions(self) -> list[dict]:
        """Get all currently open positions."""
        sql = "SELECT * FROM positions WHERE status = 'open'"
        cursor = await self.connection.execute(sql)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_open_position_count(self) -> int:
        """Get the number of currently open positions (for limit checking)."""
        sql = "SELECT COUNT(*) as count FROM positions WHERE status = 'open'"
        cursor = await self.connection.execute(sql)
        row = await cursor.fetchone()
        return row["count"] if row else 0

    async def get_position_by_token(self, token_mint: str) -> dict | None:
        """Check if we already have an open position in a specific token."""
        sql = "SELECT * FROM positions WHERE token_mint = ? AND status = 'open'"
        cursor = await self.connection.execute(sql, (token_mint,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def close_position(self, position_id: int, reason: str, realized_pnl: float) -> None:
        """Close a position and record why."""
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE positions SET
                status = 'closed', close_reason = ?, realized_pnl_sol = ?,
                closed_at = ?
            WHERE id = ?
        """
        await self.connection.execute(sql, (reason, realized_pnl, now, position_id))
        await self.connection.commit()

    async def update_position_price(self, position_id: int, current_price: float, unrealized_pnl: float) -> None:
        """Update a position's current price and unrealized PnL."""
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE positions SET
                current_price_usd = ?, unrealized_pnl_sol = ?, last_checked_at = ?
            WHERE id = ?
        """
        await self.connection.execute(sql, (current_price, unrealized_pnl, now, position_id))
        await self.connection.commit()

    # =========================================================================
    # Daily Stats (Stage 5: Telegram summaries)
    # =========================================================================

    async def update_daily_stats(self) -> dict:
        """Calculate and save today's stats. Returns the stats dict."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = await self.get_todays_trades()

        total_pnl = await self.get_todays_pnl()
        buy_count = sum(1 for t in trades if t["side"] == "buy")
        sell_count = sum(1 for t in trades if t["side"] == "sell")
        wins = sum(1 for t in trades if t["side"] == "sell" and (t.get("amount_sol") or 0) > 0)

        stats = {
            "date": today,
            "trades_executed": len(trades),
            "positions_opened": buy_count,
            "positions_closed": sell_count,
            "total_pnl_sol": total_pnl,
        }

        sql = """
            INSERT INTO daily_stats (date, trades_executed, positions_opened, positions_closed, total_pnl_sol)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                trades_executed = excluded.trades_executed,
                positions_opened = excluded.positions_opened,
                positions_closed = excluded.positions_closed,
                total_pnl_sol = excluded.total_pnl_sol
        """
        await self.connection.execute(sql, (
            today, len(trades), buy_count, sell_count, total_pnl
        ))
        await self.connection.commit()

        return stats

    # =========================================================================
    # Cluster Operations (Stage 6: Cluster Detection)
    # =========================================================================

    async def create_cluster(self, cluster_data: dict[str, Any]) -> int:
        """Create a new wallet cluster. Returns the cluster ID."""
        sql = """
            INSERT INTO wallet_clusters (
                seed_wallet, cluster_label, total_members,
                best_side_wallet, avg_lead_time_seconds
            ) VALUES (?, ?, ?, ?, ?)
        """
        cursor = await self.connection.execute(sql, (
            cluster_data["seed_wallet"],
            cluster_data.get("cluster_label"),
            cluster_data.get("total_members", 0),
            cluster_data.get("best_side_wallet"),
            cluster_data.get("avg_lead_time_seconds", 0),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def add_cluster_member(self, member_data: dict[str, Any]) -> int:
        """Add a wallet to a cluster. Returns the member record ID."""
        sql = """
            INSERT INTO wallet_cluster_members (
                cluster_id, wallet_address, relationship_type,
                is_side_wallet, confidence, avg_lead_time_seconds, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.connection.execute(sql, (
            member_data["cluster_id"],
            member_data["wallet_address"],
            member_data["relationship_type"],
            member_data.get("is_side_wallet", False),
            member_data.get("confidence", 0),
            member_data.get("avg_lead_time_seconds", 0),
            member_data.get("evidence"),
        ))
        await self.connection.commit()
        return cursor.lastrowid

    async def get_cluster_by_seed(self, seed_wallet: str) -> dict | None:
        """Look up if we've already analyzed this seed wallet."""
        sql = "SELECT * FROM wallet_clusters WHERE seed_wallet = ?"
        cursor = await self.connection.execute(sql, (seed_wallet,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_cluster_members(self, cluster_id: int) -> list[dict]:
        """Get all members of a specific cluster."""
        sql = "SELECT * FROM wallet_cluster_members WHERE cluster_id = ? ORDER BY confidence DESC"
        cursor = await self.connection.execute(sql, (cluster_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_side_wallets(self) -> list[dict]:
        """Get all identified side wallets across all clusters."""
        sql = """
            SELECT wcm.*, wc.seed_wallet, w.total_score, w.is_monitored
            FROM wallet_cluster_members wcm
            JOIN wallet_clusters wc ON wcm.cluster_id = wc.id
            LEFT JOIN wallets w ON wcm.wallet_address = w.address
            WHERE wcm.is_side_wallet = TRUE
            ORDER BY wcm.confidence DESC
        """
        cursor = await self.connection.execute(sql)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_all_clusters(self) -> list[dict]:
        """Get all clusters with summary info."""
        sql = """
            SELECT wc.*, w.total_score as seed_score
            FROM wallet_clusters wc
            LEFT JOIN wallets w ON wc.seed_wallet = w.address
            ORDER BY wc.avg_lead_time_seconds DESC
        """
        cursor = await self.connection.execute(sql)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_wallet_token_trades_for_wallet(self, wallet_address: str) -> list[dict]:
        """Get all token trades for a specific wallet."""
        sql = "SELECT * FROM wallet_token_trades WHERE wallet_address = ?"
        cursor = await self.connection.execute(sql, (wallet_address,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
