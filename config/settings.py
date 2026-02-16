"""
Store configuration — loads credentials from .env file.
"""

import os
from dotenv import load_dotenv

# Load .env from project root
load_dotenv()

# ─────────────────────────────────────────────
# WooCommerce API
# ─────────────────────────────────────────────
WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")

# ────────────────────────��────────────────────
# API Defaults
# ─────────────────────────────────────────────
DEFAULT_PER_PAGE = 20
DEFAULT_STATUS = "publish"
DEFAULT_STOCK_STATUS = "instock"
REQUEST_TIMEOUT = 30  # seconds

# ─────────────────────────────────────────────
# App Settings
# ─────────────────────────────────────────────
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")