import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("NANOGATEWAY")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_LOG_PATH = PROJECT_ROOT / ".nanogateway" / "nanogateway.log"
GATEWAY_HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 10.0


def gateway_base_url(port: int) -> str:
    return f"http://{GATEWAY_HOST}:{port}"


def _open_gateway_log():
    GATEWAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    return open(GATEWAY_LOG_PATH, "ab")


def is_gateway_up(port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((GATEWAY_HOST, port)) == 0


def start_gateway(port: int, upstream: str | None) -> subprocess.Popen | None:
    """Start ``nanogateway serve`` unless something already listens on ``port``.

    Returns the Popen handle when this call started the process, or ``None``
    when a gateway was already running (in which case the caller does not own
    it and must not stop it).
    """
    if is_gateway_up(port):
        logger.info("NanoGateway already listening on port %d; reusing it", port)
        return None

    cmd = [sys.executable, "-m", "nanogateway", "serve", "--port", str(port)]
    env = os.environ.copy()
    if upstream:
        env["NANOGATEWAY_URL"] = upstream
    logger.info(
        "Starting NanoGateway on port %d (upstream=%s, log=%s)",
        port,
        upstream or "default",
        GATEWAY_LOG_PATH,
    )
    log_file = _open_gateway_log()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        logger.error("Failed to launch NanoGateway: %s", e)
        return None
    finally:
        log_file.close()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logger.error("NanoGateway exited during startup (code %s)", proc.returncode)
            return None
        if is_gateway_up(port):
            logger.info("NanoGateway ready at %s", gateway_base_url(port))
            return proc
        time.sleep(0.1)

    logger.error("NanoGateway not ready within %.0fs", STARTUP_TIMEOUT_SECONDS)
    stop_gateway(proc)
    return None


def stop_gateway(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    logger.info("Stopping NanoGateway (pid %d)", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
