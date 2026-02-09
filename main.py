"""
Rome Agent Trader — Main Entry Point
=====================================
This is where everything starts. Running this file:
1. Loads your configuration from .env
2. Validates that all required API keys are present
3. Connects to the database
4. Initializes the Solana client
5. Starts whichever modules are enabled

Usage:
    python main.py                  # Normal startup
    python main.py --discover       # Run token discovery only
    python main.py --analyze        # Run wallet analysis only
    python main.py --clusters       # Detect wallet clusters + side wallets
    python main.py --dry-run        # Start in dry-run mode (no real trades)
    python main.py --dashboard      # Launch the web dashboard
"""

import asyncio
import argparse
import sys

from config.settings import settings
from database.db import Database
from utils.logger import setup_logging, get_logger
from utils.solana_client import SolanaClient

logger = get_logger(__name__)


async def startup_checks(db: Database, solana: SolanaClient) -> bool:
    """
    Run checks before the bot starts trading.
    Returns True if everything looks good, False if there's a problem.
    """
    logger.info("running_startup_checks")
    all_good = True

    # Check 1: Validate settings
    problems = settings.validate()
    if problems:
        for problem in problems:
            logger.warning("config_issue", issue=problem)
        # Only block startup if we're in live mode and missing critical keys
        if settings.trading_mode == "live":
            logger.error("cannot_start_live_mode", issues=len(problems))
            all_good = False

    # Check 2: Check wallet balance (if we have a wallet loaded)
    if solana.wallet_address:
        balance = await solana.get_sol_balance(solana.wallet_address)
        logger.info("wallet_balance", address=solana.wallet_address, balance_sol=f"{balance:.4f}")
        if balance < 0.01 and settings.trading_mode == "live":
            logger.warning("low_balance", balance_sol=balance, note="Need SOL for trading + fees")
    else:
        if settings.trading_mode == "live":
            logger.error("no_wallet", note="WALLET_PRIVATE_KEY not set, can't trade")
            all_good = False
        else:
            logger.info("no_wallet", note="Running without wallet (ok for discovery/analysis)")

    return all_good


async def main() -> None:
    """Main async entry point."""

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Rome Agent Trader")
    parser.add_argument("--discover", action="store_true", help="Run token discovery only")
    parser.add_argument("--analyze", action="store_true", help="Run wallet analysis only")
    parser.add_argument("--dry-run", action="store_true", help="Start in dry-run mode")
    parser.add_argument("--clusters", action="store_true", help="Detect wallet clusters and side wallets")
    parser.add_argument("--dashboard", action="store_true", help="Launch web dashboard")
    parser.add_argument("--mode", choices=["live", "dry_run", "alert_only"], help="Override trading mode")
    parser.add_argument("--import-smart-money", action="store_true", help="Import smart money wallets from GMGN")
    parser.add_argument("--add-wallet", type=str, nargs="+", help="Add wallet address(es) manually")
    parser.add_argument("--source", type=str, default="manual", help="Source label for --add-wallet (fomo, gmgn, manual)")
    parser.add_argument("--wipe-wallets", action="store_true", help="Delete all wallets and start fresh")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    # Override trading mode if specified
    if args.dry_run:
        settings.trading_mode = "dry_run"
    if args.mode:
        settings.trading_mode = args.mode

    # Set up logging
    setup_logging(log_level=settings.log_level, log_dir="logs")

    logger.info(
        "bot_starting",
        mode=settings.trading_mode,
        position_size=f"{settings.default_position_size_sol} SOL",
        max_positions=settings.max_open_positions,
    )

    # Initialize core components
    db = Database(settings.db_path)
    await db.initialize()

    solana = SolanaClient(
        helius_api_key=settings.helius_api_key,
        wallet_private_key=settings.wallet_private_key if settings.trading_mode == "live" else None,
    )
    await solana.initialize()

    # Run startup checks
    checks_passed = await startup_checks(db, solana)

    if not checks_passed and settings.trading_mode == "live":
        logger.error("startup_checks_failed", note="Fix the issues above and restart")
        await solana.close()
        await db.close()
        sys.exit(1)

    # =========================================================================
    # Module Startup — Each stage adds a new section here
    # =========================================================================

    try:
        # =================================================================
        # Wallet Management Commands
        # =================================================================

        if args.wipe_wallets:
            # Wipe all wallet data to start fresh
            if not args.yes:
                confirm = input("This will DELETE all wallets, token trades, and clusters. Type 'yes' to confirm: ")
                if confirm.lower() != "yes":
                    print("Aborted.")
                    return
            counts = await db.wipe_wallets()
            total = sum(counts.values())
            logger.info("wallets_wiped", **counts)
            print(f"\nWiped {total} records:")
            for table, count in counts.items():
                print(f"  {table}: {count} rows deleted")
            print("\nDatabase is clean. Run --import-smart-money to populate with fresh data.")
            return

        elif args.add_wallet:
            # Manually add wallet(s) with GMGN enrichment
            from discovery.gmgn_client import GMGNClient

            gmgn = GMGNClient(
                cf_clearance=settings.gmgn_cf_clearance,
                cf_bm=settings.gmgn_cf_bm,
            )

            source = args.source.lower()
            added = 0

            for address in args.add_wallet:
                address = address.strip()
                if len(address) < 30:
                    print(f"  Skipping invalid address: {address}")
                    continue

                print(f"  Enriching {address[:8]}...{address[-4:]} via GMGN...")
                stats = await gmgn.get_wallet_stats(address)

                def _f(val, default=0.0):
                    if val is None: return default
                    try: return float(val)
                    except (ValueError, TypeError): return default

                profit_30d = _f(stats.get("realized_profit_30d"))
                winrate = stats.get("winrate")
                tags = stats.get("tags") or []
                if isinstance(tags, str):
                    import json as _json
                    try: tags = _json.loads(tags)
                    except: tags = []

                wallet_data = {
                    "address": address,
                    "source": source,
                    "total_score": 50,  # Baseline — manually added wallets start at 50
                    "gmgn_realized_profit_usd": _f(stats.get("realized_profit")),
                    "gmgn_profit_30d_usd": profit_30d,
                    "gmgn_sol_balance": _f(stats.get("sol_balance")),
                    "gmgn_winrate": _f(winrate) if winrate is not None else None,
                    "gmgn_buy_30d": int(_f(stats.get("buy_30d"))),
                    "gmgn_sell_30d": int(_f(stats.get("sell_30d"))),
                    "gmgn_tags": tags,
                    "is_monitored": True,
                }
                await db.upsert_wallet(wallet_data)
                added += 1

                wr_display = f"{_f(winrate)*100:.0f}%" if winrate is not None else "—"
                print(f"  ✓ Added {address[:8]}...{address[-4:]} | 30D: ${profit_30d:,.0f} | WR: {wr_display} | Source: {source.upper()}")

            gmgn.close()
            print(f"\nDone. Added {added} wallet(s) — all set to MONITORED.")
            return

        elif getattr(args, "import_smart_money", False):
            # Import smart money wallets from GMGN
            logger.info("mode_import_smart_money")
            from discovery.gmgn_client import GMGNClient

            gmgn = GMGNClient(
                cf_clearance=settings.gmgn_cf_clearance,
                cf_bm=settings.gmgn_cf_bm,
            )

            if not gmgn.is_authenticated:
                print("ERROR: GMGN cookies not set. Add GMGN_CF_CLEARANCE and GMGN_CF_BM to .env")
                print("  1. Go to https://gmgn.ai in Chrome")
                print("  2. DevTools → Application → Cookies → gmgn.ai")
                print("  3. Copy cf_clearance and __cf_bm values to .env")
                gmgn.close()
                return

            print("Scanning GMGN for smart money wallets...")
            print(f"  Filters: profit_30d > ${settings.sm_min_profit_30d_usd:,.0f}, "
                  f"winrate > {settings.sm_min_winrate*100:.0f}%, "
                  f"buys > {settings.sm_min_buys_30d}, "
                  f"balance > {settings.sm_min_sol_balance} SOL")

            wallets = await gmgn.get_smart_money_wallets(
                min_profit_30d=settings.sm_min_profit_30d_usd,
                min_winrate=settings.sm_min_winrate,
                min_buys_30d=settings.sm_min_buys_30d,
                max_buys_30d=settings.sm_max_buys_30d,
                min_sol_balance=settings.sm_min_sol_balance,
            )
            gmgn.close()

            if not wallets:
                print("\nNo wallets passed filters. Try lowering thresholds in .env:")
                print("  SM_MIN_PROFIT_30D_USD=500")
                print("  SM_MIN_WINRATE=0.3")
                return

            # Save to database
            monitored_count = 0
            for i, w in enumerate(wallets):
                w["source"] = "gmgn"
                w["total_score"] = 50  # Baseline — will be refined by --analyze
                # Auto-monitor top N
                if i < settings.sm_auto_monitor_top:
                    w["is_monitored"] = True
                    monitored_count += 1
                else:
                    w["is_monitored"] = False
                await db.upsert_wallet(w)

            print(f"\n{'='*60}")
            print(f"  Smart Money Import Complete")
            print(f"  Total passed filters: {len(wallets)}")
            print(f"  Auto-monitored: {monitored_count}")
            print(f"{'='*60}")
            print(f"\nTop 5 wallets:")
            for i, w in enumerate(wallets[:5]):
                wr = w.get("gmgn_winrate")
                wr_str = f"{wr*100:.0f}%" if wr is not None else "—"
                tags = w.get("gmgn_tags", [])
                tag_str = ", ".join(tags[:3]) if tags else "—"
                print(f"  {i+1}. {w['address'][:8]}...{w['address'][-4:]} | "
                      f"30D: ${w['gmgn_profit_30d_usd']:,.0f} | "
                      f"WR: {wr_str} | "
                      f"Tags: {tag_str}")
            print(f"\nRefresh dashboard to see them in Smart Money tab.")
            return

        elif args.discover:
            # Stage 1: Run token discovery only
            # Finds the best-performing Solana tokens from the last 30 days
            logger.info("mode_discovery_only")
            from discovery.token_scanner import TokenScanner
            from discovery.token_filter import TokenFilter

            scanner = TokenScanner(settings, db)
            await scanner.initialize()
            tokens = await scanner.run_discovery()

            # Apply quality filters
            token_filter = TokenFilter(settings)
            quality_tokens = token_filter.apply_filters(tokens)
            logger.info("discovery_finished", total_found=len(tokens), after_quality_filter=len(quality_tokens))

            await scanner.close()

        elif args.analyze:
            # Stage 2: Run wallet analysis on previously discovered tokens
            # First discover tokens (or use cached), then find + score wallets
            logger.info("mode_analysis")

            from discovery.token_scanner import TokenScanner
            from discovery.token_filter import TokenFilter
            from analyzer.wallet_finder import WalletFinder
            from analyzer.wallet_scorer import WalletScorer
            from analyzer.anomaly_detector import AnomalyDetector

            # Step 1: Get tokens to analyze (from DB or fresh discovery)
            top_tokens = await db.get_top_tokens(limit=settings.max_discovery_tokens)
            if not top_tokens:
                logger.info("no_cached_tokens", note="Running discovery first...")
                scanner = TokenScanner(settings, db)
                await scanner.initialize()
                top_tokens = await scanner.run_discovery()
                token_filter = TokenFilter(settings)
                top_tokens = token_filter.apply_filters(top_tokens)
                await scanner.close()

            # Step 2: Find wallets that were early on these tokens
            finder = WalletFinder(settings, db, solana)
            await finder.initialize()
            wallet_data = await finder.find_smart_wallets(top_tokens)
            await finder.close()

            # Step 3: Score the wallets
            scorer = WalletScorer(settings, db)
            scored_wallets = await scorer.score_wallets(wallet_data)

            # Step 4: Flag suspicious wallets (bots, insiders, devs)
            detector = AnomalyDetector(settings)
            scored_wallets = detector.analyze(scored_wallets, wallet_data)

            # Save flags back to DB (anomaly detector modifies in memory only)
            flagged_count = 0
            for w in scored_wallets:
                if w.get("is_flagged"):
                    await db.upsert_wallet(w)
                    flagged_count += 1
            if flagged_count:
                logger.info("flags_saved_to_db", count=flagged_count)

            # Step 5: Auto-select the best clean wallets for monitoring
            monitored = await scorer.auto_select_monitored_wallets(
                [w for w in scored_wallets if not w.get("is_flagged")]
            )
            logger.info("analysis_complete", monitored_wallets=len(monitored))

        elif args.clusters:
            # Stage 6: Detect wallet clusters and side wallets
            # Requires --analyze to have been run first (needs scored wallets in DB)
            logger.info("mode_cluster_detection")

            from analyzer.cluster_detector import ClusterDetector
            from analyzer.platform_scraper import PlatformScraper

            # Step 1 (optional): Fetch extra seed wallets from platform leaderboards
            if settings.bitquery_api_key:
                logger.info("fetching_platform_wallets")
                scraper = PlatformScraper(settings, db)
                await scraper.initialize()
                platform_wallets = await scraper.fetch_top_traders(limit=100)
                await scraper.close()
                logger.info("platform_wallets_fetched", count=len(platform_wallets))
            else:
                logger.info("platform_scraper_skipped", reason="No BITQUERY_API_KEY set")

            # Step 2: Get top-scored wallets from DB as seed wallets
            seed_rows = await db.get_top_wallets(limit=settings.max_cluster_seeds)
            seed_wallets = [w["address"] for w in seed_rows if w.get("address")]

            if not seed_wallets:
                logger.error("no_seed_wallets", note="Run --analyze first to score wallets")
                return

            logger.info("cluster_seeds", count=len(seed_wallets))

            # Step 3: Run cluster detection
            detector = ClusterDetector(settings, db, solana)
            await detector.initialize()
            clusters = await detector.detect_clusters(seed_wallets)
            await detector.close()

            # Step 4: Report results
            side_wallets = await db.get_side_wallets()
            logger.info(
                "cluster_detection_complete",
                clusters_found=len(clusters),
                side_wallets_promoted=len(side_wallets),
                note="Side wallets are now monitored. Run the bot to start copying them.",
            )

        else:
            # Full bot mode — start all modules concurrently
            logger.info("mode_full_bot")

            from monitor.wallet_monitor import WalletMonitor
            from monitor.signal_generator import SignalGenerator
            from trader.trade_executor import TradeExecutor
            from trader.position_manager import PositionManager
            from telegram_bot.bot import TelegramBot
            from telegram_bot.notifier import TelegramNotifier

            # Initialize trade executor
            executor = TradeExecutor(settings, db, solana)
            await executor.initialize()

            # Initialize signal generator (validates signals before trading)
            sig_gen = SignalGenerator(settings, db)
            await sig_gen.initialize()

            # Initialize position manager (monitors TP/SL)
            pos_manager = PositionManager(settings, db, executor)
            await pos_manager.initialize()

            # Initialize Telegram bot (command handler) and notifier (push alerts)
            tg_bot = TelegramBot(settings, db)
            await tg_bot.initialize()

            notifier = TelegramNotifier(settings)
            await notifier.initialize()

            # Give the executor access to the notifier for sell alerts
            executor.notifier = notifier

            # Signal handler: when a wallet buys, validate and maybe copy
            async def on_wallet_signal(signal: dict) -> None:
                should_trade, enriched, skip_reason = await sig_gen.validate_signal(signal)
                if should_trade:
                    result = await executor.handle_signal(enriched)
                    if result:
                        logger.info("copy_trade_result", status=result.get("status"))
                        # Push Telegram alert for executed trade
                        await notifier.notify_buy({
                            "token_symbol": enriched.get("token_symbol"),
                            "amount_sol": result.get("amount_sol"),
                            "price_usd": result.get("price_usd"),
                            "triggered_by_wallet": enriched.get("wallet_address", ""),
                            "status": result.get("status"),
                            "tx_signature": result.get("tx_signature", ""),
                        })
                else:
                    logger.info("signal_skipped", reason=skip_reason)
                    await notifier.notify_signal_skipped(signal, skip_reason)

            # Initialize wallet monitor with our signal handler
            monitor = WalletMonitor(settings, db, solana, on_signal=on_wallet_signal)
            await monitor.initialize()

            # Send startup notification
            await notifier.notify_startup()

            logger.info(
                "bot_ready",
                trading_mode=settings.trading_mode,
                position_size=f"{settings.default_position_size_sol} SOL",
                note="All systems running. Monitoring wallets.",
            )

            # Run monitor, position manager, and Telegram bot concurrently
            try:
                await asyncio.gather(
                    monitor.start(),
                    pos_manager.start(),
                    tg_bot.start(),
                )
            except asyncio.CancelledError:
                pass

            # Cleanup
            await tg_bot.stop()
            await sig_gen.close()
            await executor.close()
            await pos_manager.close()

    except KeyboardInterrupt:
        logger.info("bot_stopping", reason="keyboard_interrupt")
    except Exception as e:
        logger.error("bot_error", error=str(e), type=type(e).__name__)
        raise
    finally:
        # Clean shutdown — always close connections properly
        logger.info("shutting_down")
        await solana.close()
        await db.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    # Dashboard runs outside async loop (uvicorn manages its own)
    import argparse as _argparse
    _pre_parser = _argparse.ArgumentParser(add_help=False)
    _pre_parser.add_argument("--dashboard", action="store_true")
    _pre_args, _ = _pre_parser.parse_known_args()

    if _pre_args.dashboard:
        from dashboard.app import run_dashboard
        setup_logging(log_level=settings.log_level, log_dir="logs")
        logger.info("launching_dashboard")
        run_dashboard()
    else:
        # asyncio.run() starts the async event loop and runs our main function
        asyncio.run(main())
