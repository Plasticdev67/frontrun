"""
Solana Copy Trading Bot — Main Entry Point
===========================================
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
    parser = argparse.ArgumentParser(description="Solana Copy Trading Bot")
    parser.add_argument("--discover", action="store_true", help="Run token discovery only")
    parser.add_argument("--analyze", action="store_true", help="Run wallet analysis only")
    parser.add_argument("--dry-run", action="store_true", help="Start in dry-run mode")
    parser.add_argument("--clusters", action="store_true", help="Detect wallet clusters and side wallets")
    parser.add_argument("--dashboard", action="store_true", help="Launch web dashboard")
    parser.add_argument("--mode", choices=["live", "dry_run", "alert_only"], help="Override trading mode")
    args = parser.parse_args()

    # Dashboard mode — launch web UI and exit (no trading)
    if args.dashboard:
        from dashboard.app import run_dashboard
        setup_logging(log_level=settings.log_level, log_dir="logs")
        logger.info("launching_dashboard")
        run_dashboard()
        return

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
        if args.discover:
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
    # asyncio.run() starts the async event loop and runs our main function
    asyncio.run(main())
