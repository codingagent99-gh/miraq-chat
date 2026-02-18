"""
chat_logger.py - Centralized logging configuration for miraq-chat

Sets up Python logging with:
- File handler: logs/YYYY-MM-DD/chat.txt (daily rotation)
- Console handler: stdout (maintains existing print-like behavior)
- Configurable log level via LOG_LEVEL env variable
- Sanitization of sensitive data (consumer keys, secrets)
"""

import os
import logging
from datetime import datetime
from pathlib import Path


def setup_logger(name: str = "miraq_chat", log_level: str = "INFO") -> logging.Logger:
    """
    Configure and return a logger with file and console handlers.
    
    Args:
        name: Logger name
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    
    Returns:
        Configured logger instance
    """
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    # Prevent duplicate handlers if setup is called multiple times
    if logger.handlers:
        return logger
    
    # Create log format
    log_format = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S.%f"
    )
    
    # Trim microseconds to 3 digits (milliseconds)
    class MillisecondFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            ct = self.converter(record.created)
            if datefmt:
                s = datetime.fromtimestamp(record.created).strftime(datefmt)
                # Trim microseconds to milliseconds
                if ".%f" in datefmt:
                    s = s[:-3]
                return s
            else:
                return super().formatTime(record, datefmt)
    
    formatter = MillisecondFormatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S.%f"
    )
    
    # ─── File Handler (daily rotation) ───
    # Create logs directory with today's date subfolder
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = Path("logs") / today
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / "chat.txt"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # Log everything to file
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # ─── Console Handler ───
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
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
    
    # Remove consumer_key and consumer_secret query params
    import re
    url = re.sub(r'consumer_key=[^&]*', 'consumer_key=***', url)
    url = re.sub(r'consumer_secret=[^&]*', 'consumer_secret=***', url)
    return url


def get_logger(name: str = "miraq_chat") -> logging.Logger:
    """
    Get the configured logger instance.
    If logger doesn't exist, create it with default settings.
    
    Args:
        name: Logger name
    
    Returns:
        Logger instance
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        # Logger not yet configured, set it up with default level
        from config.settings import LOG_LEVEL
        setup_logger(name, LOG_LEVEL)
    return logger
