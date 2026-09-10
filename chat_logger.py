"""
chat_logger.py - Centralized logging configuration for miraq-chat

Sets up Python logging with:
- File handler: <project_root>/logs/YYYY-MM-DD/chat.txt (daily rotation, absolute path)
- Console handler: stdout (maintains existing print-like behavior)
- Configurable log level via LOG_LEVEL env variable
- Sanitization of sensitive data (consumer keys, secrets)
"""

import os
import re
import logging
import threading
from datetime import datetime
from pathlib import Path

# ── Anchor log directory to THIS file's location, not the process CWD ──────
# Previously used Path("logs") which is relative to wherever you launch the
# server from — so if the CWD changed, logs silently went somewhere else.
_PROJECT_ROOT = Path(__file__).resolve().parent
_LOG_BASE_DIR = _PROJECT_ROOT / "logs"

# Guards handler creation for every get_*_logger() below. Each one does
# check-then-add ("if logger.handlers: return; else attach handlers") which
# is not atomic. Under real concurrency -- e.g. 20 load-test threads hitting
# an endpoint for the first time at once -- several threads pass the empty
# check before any of them attaches a handler, so the logger ends up with N
# handlers and every line after that logs N times. One lock across all
# loggers is fine: this only matters at first-call setup, never on the hot
# path of an already-configured logger.
_handler_setup_lock = threading.Lock()


def sanitize_log_string(text: str) -> str:
    """
    Sanitize string for logging to prevent log injection attacks.
    Removes newlines, carriage returns, and other control characters.

    Args:
        text: String to sanitize

    Returns:
        Sanitized string safe for logging
    """
    if not text:
        return text
    # Replace newlines, carriage returns, and tabs with spaces
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    # Remove other control characters (ASCII 0-31 except space)
    text = ''.join(char if ord(char) >= 32 or char == ' ' else ' ' for char in text)
    return text


class _MillisecondFormatter(logging.Formatter):
    """Formatter that appends milliseconds to the timestamp."""
    def formatTime(self, record, datefmt=None):
        if datefmt:
            import time
            ct = self.converter(record.created)
            s = time.strftime(datefmt, ct)
            ms = int((record.created - int(record.created)) * 1000)
            return f"{s}.{ms:03d}"
        return super().formatTime(record, datefmt)


class _DailyDirectoryHandler(logging.FileHandler):
    """
    File handler that rotates into a new YYYY-MM-DD subdirectory at midnight
    without requiring a server restart.
    """
    def __init__(self, log_base_dir: Path, filename: str, **kwargs):
        self._log_base_dir = log_base_dir
        self._filename = filename
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        log_file = self._make_log_file(self._current_date)
        super().__init__(log_file, encoding="utf-8", **kwargs)

    def _make_log_file(self, date_str: str) -> Path:
        log_dir = self._log_base_dir / date_str
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / self._filename

    def emit(self, record):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self.close()
            self._current_date = today
            self.baseFilename = str(self._make_log_file(today))
            self.stream = self._open()
        super().emit(record)


def setup_logger(name: str = "miraq_chat", log_level: str = "INFO") -> logging.Logger:
    """
    Configure and return a logger with file and console handlers.

    Safe to call multiple times — duplicate handlers are skipped.

    Args:
        name:      Logger name
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # ── Stop records bubbling up to the root logger ──────────────────────────
    # Without this, Flask/Werkzeug's root handlers can swallow or duplicate
    # your records, making it look like logging has stopped.
    logger.propagate = False

    # Guard: don't add handlers a second time (e.g. Flask debug reloader).
    # Lock + re-check inside: the check above alone is not atomic under
    # concurrent first calls -- see _handler_setup_lock's docstring.
    with _handler_setup_lock:
        if logger.handlers:
            return logger

        formatter = _MillisecondFormatter(
            fmt="[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # ── File Handler (daily rotation, absolute path) ──────────────────
        try:
            file_handler = _DailyDirectoryHandler(_LOG_BASE_DIR, "chat.txt")
            file_handler.setLevel(logging.DEBUG)   # capture everything in file
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            # Fall back gracefully — at least console logging will still work
            print(f"[chat_logger] WARNING: Could not create log file: {exc}")

        # ── Console Handler ────────────────────────────────────────────────
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        logger.debug(f"Logger '{name}' initialised | level={log_level.upper()}")
        return logger


def sanitize_url(url: str) -> str:
    """
    Remove sensitive query parameters from URLs.
    Strips consumer_key and consumer_secret.

    Args:
        url: URL string potentially containing sensitive params

    Returns:
        Sanitized URL string
    """
    if not url:
        return url
    url = re.sub(r'consumer_key=[^&]*', 'consumer_key=***', url)
    url = re.sub(r'consumer_secret=[^&]*', 'consumer_secret=***', url)
    return url


def get_logger(name: str = "miraq_chat") -> logging.Logger:
    """
    Get the configured logger instance.
    Creates and configures it on first call; returns cached instance thereafter.

    Args:
        name: Logger name

    Returns:
        Logger instance
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        log_level = os.getenv("LOG_LEVEL", "INFO")
        setup_logger(name, log_level)  # setup_logger takes the lock itself
    return logger


def get_api_logger() -> logging.Logger:
    """
    Get a dedicated logger for outbound WooCommerce API calls.

    Writes to logs/YYYY-MM-DD/api.txt — separate from chat.txt so API
    traffic can be tailed or parsed independently.

    Always logs at DEBUG level to file so full request bodies are captured.
    Console output follows the LOG_LEVEL env var, same as the main logger.

    Returns:
        Logger instance named "miraq_api"
    """
    name = "miraq_api"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    # Lock + re-check: the bare check above races under concurrent first
    # calls. Everything that mutates the logger -- setLevel, propagate, AND
    # every addHandler -- must stay inside the lock together; a handler
    # added outside it is exactly the race this exists to close.
    with _handler_setup_lock:
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        formatter = _MillisecondFormatter(
            fmt="[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        try:
            file_handler = _DailyDirectoryHandler(_LOG_BASE_DIR, "api.txt")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            print(f"[chat_logger] WARNING: Could not create api log file: {exc}")

        # Console: only show at WARNING+ by default to keep stdout clean
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger


def get_order_logger() -> logging.Logger:
    """
    Get a dedicated logger for WooCommerce order-creation calls (POST /orders,
    admin surface only — not the order list/fetch calls, which stay on the
    general API logger).

    Writes to logs/YYYY-MM-DD/orders.txt — separate from both chat.txt and
    api.txt, so order payload/response traffic can be tailed or parsed on
    its own.

    Always logs at DEBUG level to file so full request/response bodies are
    captured. Console output follows the LOG_LEVEL env var, same as the main
    logger.

    Returns:
        Logger instance named "miraq_orders"
    """
    name = "miraq_orders"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    # Lock + re-check: the bare check above races under concurrent first
    # calls. Everything that mutates the logger stays inside the lock.
    with _handler_setup_lock:
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        formatter = _MillisecondFormatter(
            fmt="[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        try:
            file_handler = _DailyDirectoryHandler(_LOG_BASE_DIR, "orders.txt")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            print(f"[chat_logger] WARNING: Could not create orders log file: {exc}")

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger


def get_timing_logger() -> logging.Logger:
    """
    Get a dedicated logger for request timing (start/completion), used for
    load-test instrumentation.

    Writes to logs/YYYY-MM-DD/timing.txt — separate from chat.txt, api.txt,
    and orders.txt, so timing lines can be read on their own without being
    interleaved with everything else a request logs.

    No console handler at all (unlike get_api_logger/get_order_logger, which
    show WARNING+ on console) -- this logger is for measuring latency, and a
    console write is itself slow enough to distort the thing being measured.

    Returns:
        Logger instance named "miraq_timing"
    """
    name = "miraq_timing"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    # Lock + re-check: the bare check above races under concurrent first
    # calls. Everything that mutates the logger stays inside the lock.
    with _handler_setup_lock:
        if logger.handlers:
            return logger

        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        formatter = _MillisecondFormatter(
            fmt="[%(asctime)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        try:
            file_handler = _DailyDirectoryHandler(_LOG_BASE_DIR, "timing.txt")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            print(f"[chat_logger] WARNING: Could not create timing log file: {exc}")

        return logger