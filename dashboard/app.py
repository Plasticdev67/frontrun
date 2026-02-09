"""
Web Dashboard
=============
A browser-based dashboard for viewing Rome Agent Trader's data.

Shows:
- Overview: key stats, recent activity, PnL chart
- Wallets: smart wallet leaderboard with scores and PnL
- Tokens: discovered tokens sorted by performance
- Trades: full trade history with outcomes
- Positions: open positions with live unrealized PnL

Run with:
    python main.py --dashboard

Then open http://localhost:8050 in your browser.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import aiosqlite

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Rome Agent Trader", docs_url=None, redoc_url=None)

# Serve static files (audio, images, etc.)
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Database path from settings
DB_PATH = settings.db_path


async def get_db() -> aiosqlite.Connection:
    """Open a database connection."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn


@app.on_event("startup")
async def run_db_migrations():
    """Ensure new columns exist in existing databases."""
    conn = await aiosqlite.connect(DB_PATH)
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
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception:
            pass  # Column already exists
    await conn.commit()
    await conn.close()


# =========================================================================
# Page Routes
# =========================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main dashboard page."""
    return templates.TemplateResponse("index.html", {"request": request})


# =========================================================================
# API Routes — JSON data for the frontend
# =========================================================================

@app.get("/api/overview")
async def api_overview():
    """Dashboard overview: key stats and recent activity."""
    db = await get_db()
    try:
        # Open positions
        cursor = await db.execute("SELECT COUNT(*) as count FROM positions WHERE status = 'open'")
        row = await cursor.fetchone()
        open_positions = row["count"] if row else 0

        # Total invested in open positions
        cursor = await db.execute(
            "SELECT COALESCE(SUM(amount_sol_invested), 0) as total FROM positions WHERE status = 'open'"
        )
        row = await cursor.fetchone()
        total_invested = float(row["total"]) if row else 0

        # Total unrealized PnL
        cursor = await db.execute(
            "SELECT COALESCE(SUM(unrealized_pnl_sol), 0) as total FROM positions WHERE status = 'open'"
        )
        row = await cursor.fetchone()
        unrealized_pnl = float(row["total"]) if row else 0

        # Monitored wallets
        cursor = await db.execute("SELECT COUNT(*) as count FROM wallets WHERE is_monitored = TRUE")
        row = await cursor.fetchone()
        monitored_wallets = row["count"] if row else 0

        # Total wallets scored
        cursor = await db.execute("SELECT COUNT(*) as count FROM wallets WHERE total_score > 0")
        row = await cursor.fetchone()
        total_wallets = row["count"] if row else 0

        # Tokens discovered
        cursor = await db.execute("SELECT COUNT(*) as count FROM tokens")
        row = await cursor.fetchone()
        total_tokens = row["count"] if row else 0

        # Today's stats
        cursor = await db.execute(
            """SELECT COALESCE(SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END), 0) as buys,
                      COALESCE(SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END), 0) as sells,
                      COALESCE(SUM(CASE WHEN side='sell' THEN amount_sol
                                        WHEN side='buy' THEN -amount_sol ELSE 0 END), 0) as pnl
               FROM trades WHERE date(created_at) = date('now') AND status IN ('confirmed', 'dry_run')"""
        )
        row = await cursor.fetchone()
        today_buys = row["buys"] if row else 0
        today_sells = row["sells"] if row else 0
        today_pnl = float(row["pnl"]) if row else 0

        # All-time realized PnL (from closed positions)
        cursor = await db.execute(
            "SELECT COALESCE(SUM(realized_pnl_sol), 0) as total FROM positions WHERE status = 'closed'"
        )
        row = await cursor.fetchone()
        all_time_pnl = float(row["total"]) if row else 0

        # Recent trades (last 10)
        cursor = await db.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
        recent_trades = [dict(r) for r in rows]

        return {
            "open_positions": open_positions,
            "total_invested": total_invested,
            "unrealized_pnl": unrealized_pnl,
            "monitored_wallets": monitored_wallets,
            "total_wallets": total_wallets,
            "total_tokens": total_tokens,
            "today_buys": today_buys,
            "today_sells": today_sells,
            "today_pnl": today_pnl,
            "all_time_pnl": all_time_pnl,
            "trading_mode": settings.trading_mode,
            "trading_paused": settings.trading_paused,
            "recent_trades": recent_trades,
        }
    finally:
        await db.close()


@app.get("/api/wallets")
async def api_wallets():
    """All scored wallets, sorted by score — includes GMGN enrichment data."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT address, total_score, pnl_score, win_rate_score, timing_score,
                      consistency_score, total_pnl_sol, total_trades, winning_trades,
                      win_rate, avg_entry_rank, unique_winners,
                      gmgn_realized_profit_usd, gmgn_profit_30d_usd, gmgn_sol_balance,
                      gmgn_winrate, gmgn_buy_30d, gmgn_sell_30d, gmgn_tags,
                      is_flagged, flag_reason, is_monitored, last_active
               FROM wallets WHERE total_score > 0
               ORDER BY total_score DESC LIMIT 200"""
        )
        rows = await cursor.fetchall()
        wallets = []
        for w in rows:
            d = dict(w)
            # Parse tags from JSON string back to list
            tags_raw = d.get("gmgn_tags") or "[]"
            try:
                d["gmgn_tags"] = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
            except (json.JSONDecodeError, TypeError):
                d["gmgn_tags"] = []
            # Use stored win_rate if available, else calculate from winning_trades
            if not d.get("win_rate"):
                total = d.get("total_trades") or 0
                d["win_rate"] = (d.get("winning_trades") or 0) / total * 100 if total > 0 else 0
            wallets.append(d)
        return {"wallets": wallets}
    finally:
        await db.close()


@app.get("/api/tokens")
async def api_tokens():
    """All discovered tokens, sorted by performance."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT mint_address, symbol, name, market_cap_usd, price_usd,
                      price_change_pct, price_multiplier, volume_24h_usd,
                      liquidity_usd, holder_count, dex_name, data_source, discovered_at
               FROM tokens ORDER BY price_multiplier DESC LIMIT 200"""
        )
        rows = await cursor.fetchall()
        return {"tokens": [dict(r) for r in rows]}
    finally:
        await db.close()


@app.get("/api/trades")
async def api_trades():
    """Trade history."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, token_mint, token_symbol, side, amount_sol, amount_tokens,
                      price_usd, triggered_by_wallet, sell_reason, tx_signature,
                      status, slippage_actual_bps, error_message, created_at
               FROM trades ORDER BY created_at DESC LIMIT 200"""
        )
        rows = await cursor.fetchall()
        return {"trades": [dict(r) for r in rows]}
    finally:
        await db.close()


@app.get("/api/positions")
async def api_positions():
    """All positions (open and closed)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, token_mint, token_symbol, entry_price_usd, current_price_usd,
                      amount_sol_invested, amount_tokens_held, stop_loss_price,
                      triggered_by_wallet, status, close_reason,
                      realized_pnl_sol, unrealized_pnl_sol, opened_at, closed_at
               FROM positions ORDER BY opened_at DESC LIMIT 200"""
        )
        rows = await cursor.fetchall()
        positions = []
        for p in rows:
            d = dict(p)
            entry = d.get("entry_price_usd") or 0
            current = d.get("current_price_usd") or 0
            d["multiplier"] = current / entry if entry > 0 and current > 0 else None
            positions.append(d)
        return {"positions": positions}
    finally:
        await db.close()


@app.get("/api/daily_pnl")
async def api_daily_pnl():
    """Daily PnL history for the chart."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT date, trades_executed, positions_opened, positions_closed, total_pnl_sol
               FROM daily_stats ORDER BY date ASC LIMIT 90"""
        )
        rows = await cursor.fetchall()
        return {"daily_pnl": [dict(r) for r in rows]}
    finally:
        await db.close()


@app.get("/api/wallet/{address}")
async def api_wallet_detail(address: str):
    """Detailed info for a single wallet: scores, token trades, copy trades."""
    db = await get_db()
    try:
        # Wallet record (includes GMGN enrichment fields)
        cursor = await db.execute(
            """SELECT address, total_score, pnl_score, win_rate_score, timing_score,
                      consistency_score, total_pnl_sol, total_trades, winning_trades,
                      win_rate, avg_entry_rank, unique_winners,
                      gmgn_realized_profit_usd, gmgn_profit_30d_usd, gmgn_sol_balance,
                      gmgn_winrate, gmgn_buy_30d, gmgn_sell_30d, gmgn_tags,
                      is_flagged, flag_reason, is_monitored, first_seen, last_active, score_updated_at
               FROM wallets WHERE address = ?""",
            (address,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Wallet not found")
        wallet = dict(row)
        # Parse tags
        tags_raw = wallet.get("gmgn_tags") or "[]"
        try:
            wallet["gmgn_tags"] = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except (json.JSONDecodeError, TypeError):
            wallet["gmgn_tags"] = []
        # Use stored win_rate if available
        if not wallet.get("win_rate"):
            total = wallet.get("total_trades") or 0
            wallet["win_rate"] = (wallet.get("winning_trades") or 0) / total * 100 if total > 0 else 0

        # Token trades by this wallet
        cursor = await db.execute(
            """SELECT token_mint, token_symbol, buy_amount_sol, sell_amount_sol,
                      pnl_sol, buy_price, sell_price, entry_rank, first_buy_at, last_sell_at
               FROM wallet_token_trades WHERE wallet_address = ?
               ORDER BY first_buy_at DESC LIMIT 100""",
            (address,),
        )
        token_trades = [dict(r) for r in await cursor.fetchall()]

        # Copy trades triggered by this wallet
        cursor = await db.execute(
            """SELECT id, token_mint, token_symbol, side, amount_sol, price_usd,
                      status, created_at
               FROM trades WHERE triggered_by_wallet = ?
               ORDER BY created_at DESC LIMIT 50""",
            (address,),
        )
        copy_trades = [dict(r) for r in await cursor.fetchall()]

        return {"wallet": wallet, "token_trades": token_trades, "copy_trades": copy_trades}
    finally:
        await db.close()


@app.get("/api/token/{mint}")
async def api_token_detail(mint: str):
    """Detailed info for a single token: metadata, wallets that traded it, copy trades."""
    db = await get_db()
    try:
        # Token record
        cursor = await db.execute("SELECT * FROM tokens WHERE mint_address = ?", (mint,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")
        token = dict(row)

        # Wallets that traded this token
        cursor = await db.execute(
            """SELECT wtt.wallet_address, wtt.buy_amount_sol, wtt.sell_amount_sol,
                      wtt.pnl_sol, wtt.entry_rank, wtt.first_buy_at,
                      w.total_score, w.is_monitored
               FROM wallet_token_trades wtt
               LEFT JOIN wallets w ON w.address = wtt.wallet_address
               WHERE wtt.token_mint = ?
               ORDER BY wtt.entry_rank ASC LIMIT 50""",
            (mint,),
        )
        wallet_trades = [dict(r) for r in await cursor.fetchall()]

        # Copy trades on this token
        cursor = await db.execute(
            """SELECT id, side, amount_sol, price_usd, status, triggered_by_wallet, created_at
               FROM trades WHERE token_mint = ?
               ORDER BY created_at DESC LIMIT 50""",
            (mint,),
        )
        copy_trades = [dict(r) for r in await cursor.fetchall()]

        return {"token": token, "wallet_trades": wallet_trades, "copy_trades": copy_trades}
    finally:
        await db.close()


@app.get("/api/wallet_score_distribution")
async def api_wallet_score_distribution():
    """Histogram of wallet scores in 5 buckets."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT
                   CASE
                       WHEN total_score >= 0 AND total_score < 20 THEN '0-20'
                       WHEN total_score >= 20 AND total_score < 40 THEN '20-40'
                       WHEN total_score >= 40 AND total_score < 60 THEN '40-60'
                       WHEN total_score >= 60 AND total_score < 80 THEN '60-80'
                       WHEN total_score >= 80 THEN '80-100'
                   END AS score_range,
                   COUNT(*) as count
               FROM wallets WHERE total_score > 0
               GROUP BY score_range ORDER BY score_range"""
        )
        rows = await cursor.fetchall()
        result = {r["score_range"]: r["count"] for r in rows if r["score_range"]}

        # Ensure all 5 buckets exist
        all_ranges = ["0-20", "20-40", "40-60", "60-80", "80-100"]
        distribution = [{"range": r, "count": result.get(r, 0)} for r in all_ranges]

        return {"distribution": distribution}
    finally:
        await db.close()


@app.get("/api/trade_stats")
async def api_trade_stats():
    """Win/loss counts, daily trade activity, portfolio allocation."""
    db = await get_db()
    try:
        # Win/loss from closed positions
        cursor = await db.execute(
            """SELECT
                   COUNT(*) as total_closed,
                   COALESCE(SUM(CASE WHEN realized_pnl_sol > 0 THEN 1 ELSE 0 END), 0) as wins,
                   COALESCE(SUM(CASE WHEN realized_pnl_sol <= 0 THEN 1 ELSE 0 END), 0) as losses,
                   COALESCE(SUM(CASE WHEN realized_pnl_sol > 0 THEN realized_pnl_sol ELSE 0 END), 0) as total_win_sol,
                   COALESCE(SUM(CASE WHEN realized_pnl_sol <= 0 THEN realized_pnl_sol ELSE 0 END), 0) as total_loss_sol
               FROM positions WHERE status = 'closed'"""
        )
        row = await cursor.fetchone()
        win_loss = {
            "wins": row["wins"] if row else 0,
            "losses": row["losses"] if row else 0,
            "total_win_sol": float(row["total_win_sol"]) if row else 0,
            "total_loss_sol": float(row["total_loss_sol"]) if row else 0,
        }

        # Daily trade activity (last 30 days)
        cursor = await db.execute(
            """SELECT date(created_at) as trade_date, COUNT(*) as trade_count,
                      SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) as buys,
                      SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) as sells
               FROM trades
               WHERE created_at >= date('now', '-30 days')
                 AND status IN ('confirmed', 'dry_run')
               GROUP BY trade_date ORDER BY trade_date ASC"""
        )
        rows = await cursor.fetchall()
        daily_activity = [
            {"date": r["trade_date"], "buys": r["buys"], "sells": r["sells"]}
            for r in rows
        ]

        # Portfolio allocation (open positions)
        cursor = await db.execute(
            """SELECT token_symbol, token_mint, amount_sol_invested
               FROM positions WHERE status = 'open'
               ORDER BY amount_sol_invested DESC"""
        )
        rows = await cursor.fetchall()
        portfolio = [
            {"token_symbol": r["token_symbol"], "token_mint": r["token_mint"],
             "amount_sol": float(r["amount_sol_invested"])}
            for r in rows
        ]

        return {
            "win_loss": win_loss,
            "daily_activity": daily_activity,
            "portfolio_allocation": portfolio,
        }
    finally:
        await db.close()


@app.get("/api/clusters")
async def api_clusters():
    """All wallet clusters with their members."""
    db = await get_db()
    try:
        # Get all clusters
        cursor = await db.execute(
            """SELECT wc.*, w.total_score as seed_score
               FROM wallet_clusters wc
               LEFT JOIN wallets w ON wc.seed_wallet = w.address
               ORDER BY wc.avg_lead_time_seconds DESC"""
        )
        clusters = [dict(r) for r in await cursor.fetchall()]

        # Get members for each cluster
        for cluster in clusters:
            cursor = await db.execute(
                """SELECT wcm.*, w.total_score, w.is_monitored
                   FROM wallet_cluster_members wcm
                   LEFT JOIN wallets w ON wcm.wallet_address = w.address
                   WHERE wcm.cluster_id = ?
                   ORDER BY wcm.confidence DESC""",
                (cluster["id"],),
            )
            cluster["members"] = [dict(r) for r in await cursor.fetchall()]

        # Summary stats
        cursor = await db.execute(
            "SELECT COUNT(*) as total FROM wallet_cluster_members WHERE is_side_wallet = TRUE"
        )
        row = await cursor.fetchone()
        side_wallet_count = row["total"] if row else 0

        return {
            "clusters": clusters,
            "total_clusters": len(clusters),
            "total_side_wallets": side_wallet_count,
        }
    finally:
        await db.close()


def run_dashboard(host: str = "0.0.0.0", port: int = 8050):
    """Start the dashboard server."""
    import uvicorn
    logger.info("dashboard_starting", url=f"http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
