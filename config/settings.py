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

# ─────────────────────────────────────────────
# API Defaults
# ─────────────────────────────────────────────
# Number of products per page in chat responses.
# This controls how many product cards are returned per server request.
# The frontend shows all of them directly (no local pagination).
# Users click "Load More" to fetch the next page from the server.
DEFAULT_PER_PAGE = int(os.getenv("CHAT_PER_PAGE", "4"))

# Number of orders per page in chat responses.
# Works the same way as DEFAULT_PER_PAGE — users can page through with "show more".
# Override via CHAT_ORDERS_PER_PAGE env var.
DEFAULT_ORDER_PER_PAGE = int(os.getenv("CHAT_ORDERS_PER_PAGE", "5"))
DEFAULT_STATUS = "publish"
DEFAULT_STOCK_STATUS = "instock"
REQUEST_TIMEOUT = 30  # seconds

# ─────────────────────────────────────────────
# App Settings
# ─────────────────────────────────────────────
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")