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
    python main.py --dry-run        # Start in dry-run mode (no real trades)
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
    parser.add_argument("--mode", choices=["live", "dry_run", "alert_only"], help="Override trading mode")
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
        if args.discover:
            # Stage 1: Run token discovery only
            logger.info("mode_discovery_only")
            # TODO: Stage 1 — from discovery.token_scanner import TokenScanner
            # scanner = TokenScanner(settings, db, solana)
            # await scanner.run()
            logger.info("token_discovery_not_yet_implemented", note="Coming in Stage 1")

        elif args.analyze:
            # Stage 2: Run wallet analysis only
            logger.info("mode_analysis_only")
            # TODO: Stage 2 — from analyzer.wallet_finder import WalletAnalyzer
            # analyzer = WalletAnalyzer(settings, db, solana)
            # await analyzer.run()
            logger.info("wallet_analysis_not_yet_implemented", note="Coming in Stage 2")

        else:
            # Full bot mode — start all active modules
            logger.info("mode_full_bot")
            logger.info(
                "bot_ready",
                trading_mode=settings.trading_mode,
                note="All systems initialized. Modules will be added as we build each stage.",
            )

            # Keep the bot running (modules will add their own loops)
            # For now, just demonstrate that everything starts up correctly
            logger.info("waiting_for_modules", note="Bot is running. Press Ctrl+C to stop.")
            try:
                await asyncio.Event().wait()  # Run forever until interrupted
            except asyncio.CancelledError:
                pass

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
