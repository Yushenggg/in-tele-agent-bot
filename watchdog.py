import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from core.gateway import gateway_base_url, is_gateway_up, start_gateway, stop_gateway

BOT_MODULE = "core.main_telegram_bot"
PROJECT_ROOT = Path(__file__).resolve().parent

RESTART_DELAY_SECONDS = 3
MAX_RESTART_DELAY_SECONDS = 60

logging.basicConfig(
    format="%(asctime)s - WATCHDOG - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("WATCHDOG")


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py", description="TeleBaseBot supervisor"
    )
    parser.add_argument(
        "--nanogateway",
        nargs="?",
        const=True,
        default=None,
        type=_parse_bool,
        help=(
            "Start a local NanoGateway proxy and route the bot through it. "
            "Overrides USE_NANOGATEWAY from .env. Pass '--nanogateway false' "
            "to force it off."
        ),
    )
    return parser.parse_args(argv)


def _start_gateway_if_enabled(use_nanogateway: bool) -> tuple[dict, subprocess.Popen | None]:
    """Return (bot_env, gateway_proc) with OPENAI_BASE_URL routed via NanoGateway."""
    env = os.environ.copy()
    env["USE_NANOGATEWAY"] = "true" if use_nanogateway else "false"
    if not use_nanogateway:
        return env, None

    from core.config import app_config
    from core.dependency_sync import NANOGATEWAY_EXTRA, ensure_extra

    ok, output = ensure_extra(NANOGATEWAY_EXTRA)
    if not ok:
        logger.error(
            "Could not install the %r extra: %s", NANOGATEWAY_EXTRA, output
        )

    port = app_config.nanogateway_port
    upstream = app_config.nanogateway_url or app_config.openai_base_url_for_client
    gateway = start_gateway(port, upstream)
    if is_gateway_up(port):
        env["OPENAI_BASE_URL"] = gateway_base_url(port)
        logger.info("Routing bot through NanoGateway at %s", env["OPENAI_BASE_URL"])
    else:
        logger.error(
            "NanoGateway is not available on port %d; bot will use OPENAI_BASE_URL directly",
            port,
        )
    return env, gateway


def main():
    args = _parse_args()

    from core.config import app_config

    use_nanogateway = (
        app_config.use_nanogateway if args.nanogateway is None else args.nanogateway
    )

    logger.info("Watchdog started. Managing bot process.")
    bot_env, gateway = _start_gateway_if_enabled(use_nanogateway)

    delay = RESTART_DELAY_SECONDS
    try:
        while True:
            logger.info("Starting bot process...")
            proc = subprocess.run(
                [sys.executable, "-m", BOT_MODULE],
                cwd=PROJECT_ROOT,
                env=bot_env,
            )
            if proc.returncode == 0:
                logger.info("Bot exited cleanly (code 0). Restarting in %ds.", delay)
                delay = RESTART_DELAY_SECONDS
            else:
                logger.warning(
                    "Bot process exited with code %d. Restarting in %ds.",
                    proc.returncode,
                    delay,
                )
            time.sleep(delay)
            delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)
    except KeyboardInterrupt:
        logger.info("Watchdog interrupted; shutting down.")
    finally:
        stop_gateway(gateway)


if __name__ == "__main__":
    main()
