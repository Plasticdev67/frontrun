"""
Database Schema
===============
Defines all the tables in our SQLite database.

Think of this as the bot's filing system:
- tokens: The winning tokens we've discovered
- wallets: The smart wallets we've identified
- wallet_token_trades: Which wallets traded which tokens (for scoring)
- signals: When a monitored wallet buys something new
- trades: Every trade the bot executes (our audit trail)
- positions: Currently open positions with TP/SL tracking
- daily_stats: End-of-day summaries

We use raw SQL (not an ORM) to keep things simple and fast.
Each table has clear columns with comments explaining what they store.
"""

# SQL statements to create all tables
# These run once when the bot first starts up

CREATE_TABLES_SQL = """

-- =============================================
-- Tokens we've discovered as top performers
-- =============================================
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Token identity
    mint_address TEXT UNIQUE NOT NULL,      -- Solana token mint address (the token's unique ID)
    symbol TEXT,                             -- Ticker symbol (e.g., "PEPE")
    name TEXT,                              -- Full name (e.g., "Pepe Token")

    -- Performance data at time of discovery
    market_cap_usd REAL,                   -- Market cap in USD when we found it
    price_usd REAL,                        -- Price when we found it
    price_change_pct REAL,                 -- % price change over our lookback period
    price_multiplier REAL,                 -- How many X it did (e.g., 10.0 = 10x)
    volume_24h_usd REAL,                   -- 24-hour trading volume in USD
    liquidity_usd REAL,                    -- Available liquidity in USD
    holder_count INTEGER,                  -- Number of unique holders

    -- Metadata
    pair_address TEXT,                     -- DEX pair/pool address
    dex_name TEXT,                         -- Which DEX it trades on (Raydium, Orca, etc.)
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    data_source TEXT                        -- Where we found it (birdeye, dexscreener)
);

-- =============================================
-- Smart wallets we've identified and scored
-- =============================================
CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Wallet identity
    address TEXT UNIQUE NOT NULL,           -- Solana wallet address

    -- Scoring (0-100 scale)
    total_score REAL DEFAULT 0,            -- Overall smart money score
    pnl_score REAL DEFAULT 0,             -- Score based on total profit/loss
    win_rate_score REAL DEFAULT 0,         -- Score based on % of winning trades
    timing_score REAL DEFAULT 0,           -- Score based on how early they buy
    consistency_score REAL DEFAULT 0,       -- Score based on consistent performance

    -- Raw stats
    total_pnl_sol REAL DEFAULT 0,          -- Total profit/loss in SOL
    total_trades INTEGER DEFAULT 0,        -- Total number of trades analyzed
    winning_trades INTEGER DEFAULT 0,      -- Number of profitable trades
    avg_entry_rank INTEGER DEFAULT 0,      -- Average position among first buyers (lower = earlier)
    unique_winners INTEGER DEFAULT 0,      -- Number of different winning tokens traded

    -- Flags
    is_flagged BOOLEAN DEFAULT FALSE,      -- True if this wallet looks suspicious
    flag_reason TEXT,                       -- Why it was flagged (bot, insider, dev, etc.)
    is_monitored BOOLEAN DEFAULT FALSE,    -- True if we're actively watching this wallet

    -- Timestamps
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP,
    score_updated_at TIMESTAMP
);

-- =============================================
-- Links wallets to their trades on winning tokens
-- Used to calculate wallet scores
-- =============================================
CREATE TABLE IF NOT EXISTS wallet_token_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    wallet_address TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_symbol TEXT,

    -- Trade details
    buy_amount_sol REAL,                   -- How much SOL they spent buying
    sell_amount_sol REAL,                  -- How much SOL they got selling
    pnl_sol REAL,                          -- Profit/loss for this specific trade
    buy_price REAL,                        -- Price they bought at
    sell_price REAL,                       -- Price they sold at (null if still holding)
    entry_rank INTEGER,                    -- They were the Nth buyer of this token

    -- Timing
    first_buy_at TIMESTAMP,
    last_sell_at TIMESTAMP,

    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
    FOREIGN KEY (token_mint) REFERENCES tokens(mint_address)
);

-- =============================================
-- Signals: when a monitored wallet does something interesting
-- =============================================
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    wallet_address TEXT NOT NULL,           -- Which wallet triggered this
    token_mint TEXT NOT NULL,               -- Which token they interacted with
    token_symbol TEXT,

    signal_type TEXT NOT NULL,              -- "buy", "sell", "large_buy", etc.
    wallet_score REAL,                     -- The wallet's score at time of signal
    confidence REAL,                       -- How confident we are (0-1)

    -- Was this signal acted on?
    executed BOOLEAN DEFAULT FALSE,        -- Did we copy this trade?
    trade_id INTEGER,                      -- Link to our trade if we did copy it
    skip_reason TEXT,                      -- Why we skipped it (if we did)

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

-- =============================================
-- Trades: every trade the bot executes (THE AUDIT TRAIL)
-- =============================================
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- What we traded
    token_mint TEXT NOT NULL,
    token_symbol TEXT,
    side TEXT NOT NULL,                     -- "buy" or "sell"

    -- Amounts
    amount_sol REAL NOT NULL,              -- SOL spent (buy) or received (sell)
    amount_tokens REAL,                    -- Tokens received (buy) or sold (sell)
    price_usd REAL,                        -- Token price at execution time

    -- Why we made this trade
    triggered_by_wallet TEXT,              -- The smart wallet that triggered this
    signal_id INTEGER,                     -- Link to the signal that caused this
    sell_reason TEXT,                       -- For sells: "take_profit", "stop_loss", "manual", etc.

    -- Execution details
    tx_signature TEXT,                     -- Solana transaction signature (proof of trade)
    status TEXT DEFAULT 'pending',         -- "pending", "confirmed", "failed"
    slippage_actual_bps INTEGER,           -- Actual slippage we experienced
    priority_fee_lamports INTEGER,         -- Priority fee we paid
    error_message TEXT,                    -- If the trade failed, why

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMP,

    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

-- =============================================
-- Positions: currently open token positions
-- =============================================
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    token_mint TEXT NOT NULL,
    token_symbol TEXT,

    -- Position details
    entry_price_usd REAL NOT NULL,         -- Average price we bought at
    current_price_usd REAL,               -- Latest known price
    amount_sol_invested REAL NOT NULL,     -- Total SOL we've spent
    amount_tokens_held REAL NOT NULL,      -- How many tokens we currently hold

    -- Take-profit / Stop-loss tracking
    take_profit_levels TEXT,               -- JSON: e.g., [{"multiplier": 2.0, "pct": 0.5, "hit": false}]
    stop_loss_price REAL,                 -- Price at which we sell everything

    -- Context
    triggered_by_wallet TEXT,              -- Which wallet prompted this position
    num_buys INTEGER DEFAULT 1,           -- How many times we've bought this token
    status TEXT DEFAULT 'open',            -- "open" or "closed"
    close_reason TEXT,                     -- "take_profit", "stop_loss", "manual", "kill_switch"

    -- PnL
    realized_pnl_sol REAL DEFAULT 0,      -- Profit/loss from partial sells
    unrealized_pnl_sol REAL DEFAULT 0,    -- Current paper profit/loss

    -- Timestamps
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    last_checked_at TIMESTAMP
);

-- =============================================
-- Daily summary stats
-- =============================================
CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT UNIQUE NOT NULL,             -- YYYY-MM-DD

    -- Trading activity
    trades_executed INTEGER DEFAULT 0,
    positions_opened INTEGER DEFAULT 0,
    positions_closed INTEGER DEFAULT 0,

    -- Performance
    total_pnl_sol REAL DEFAULT 0,
    total_pnl_usd REAL DEFAULT 0,
    best_trade_pnl_sol REAL DEFAULT 0,
    worst_trade_pnl_sol REAL DEFAULT 0,

    -- Risk
    max_drawdown_sol REAL DEFAULT 0,
    hit_daily_loss_limit BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================
-- Wallet clusters: groups of linked wallets
-- controlled by the same operator
-- =============================================
CREATE TABLE IF NOT EXISTS wallet_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    seed_wallet TEXT NOT NULL,             -- The "public" wallet from leaderboards
    cluster_label TEXT,                    -- Human-readable label
    total_members INTEGER DEFAULT 0,      -- How many wallets in this cluster
    best_side_wallet TEXT,                 -- Wallet with longest avg lead time
    avg_lead_time_seconds REAL DEFAULT 0, -- Average seconds the side wallet buys ahead

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (seed_wallet) REFERENCES wallets(address)
);

-- =============================================
-- Members within each wallet cluster
-- =============================================
CREATE TABLE IF NOT EXISTS wallet_cluster_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    cluster_id INTEGER NOT NULL,
    wallet_address TEXT NOT NULL,

    relationship_type TEXT NOT NULL,       -- "funding_source", "funding_dest",
                                          -- "transfer_partner", "timing_correlated", "token_overlap"
    is_side_wallet BOOLEAN DEFAULT FALSE,  -- True if this is the early accumulator
    confidence REAL DEFAULT 0,            -- 0.0-1.0 confidence in this link
    avg_lead_time_seconds REAL DEFAULT 0, -- How far ahead this wallet buys (seconds)
    evidence TEXT,                        -- JSON with relationship details

    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (cluster_id) REFERENCES wallet_clusters(id)
);

-- =============================================
-- Indexes for fast lookups
-- =============================================
CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(total_score DESC);
CREATE INDEX IF NOT EXISTS idx_wallets_monitored ON wallets(is_monitored);
CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_mint);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_wallet_token_trades_wallet ON wallet_token_trades(wallet_address);
CREATE INDEX IF NOT EXISTS idx_wallet_token_trades_token ON wallet_token_trades(token_mint);
CREATE INDEX IF NOT EXISTS idx_clusters_seed ON wallet_clusters(seed_wallet);
CREATE INDEX IF NOT EXISTS idx_cluster_members_cluster ON wallet_cluster_members(cluster_id);
CREATE INDEX IF NOT EXISTS idx_cluster_members_wallet ON wallet_cluster_members(wallet_address);
CREATE INDEX IF NOT EXISTS idx_cluster_members_side ON wallet_cluster_members(is_side_wallet);

"""
