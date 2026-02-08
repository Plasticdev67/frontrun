"""
Web Dashboard
=============
A browser-based dashboard for viewing your copy trading bot's data.

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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import aiosqlite

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Copy Trading Dashboard", docs_url=None, redoc_url=None)

# Database path from settings
DB_PATH = settings.db_path


async def get_db() -> aiosqlite.Connection:
    """Open a read-only database connection."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn


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
    """All scored wallets, sorted by score."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT address, total_score, pnl_score, win_rate_score, timing_score,
                      consistency_score, total_pnl_sol, total_trades, winning_trades,
                      avg_entry_rank, unique_winners, is_flagged, flag_reason,
                      is_monitored, last_active
               FROM wallets WHERE total_score > 0
               ORDER BY total_score DESC LIMIT 200"""
        )
        rows = await cursor.fetchall()
        wallets = []
        for w in rows:
            d = dict(w)
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


def run_dashboard(host: str = "0.0.0.0", port: int = 8050):
    """Start the dashboard server."""
    import uvicorn
    logger.info("dashboard_starting", url=f"http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
