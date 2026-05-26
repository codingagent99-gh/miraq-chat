"""
store_loader/config.py — Environment variables and constants for StoreLoader.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── WooCommerce ───────────────────────────────────────────────────────────────

_WP_BASE = os.getenv("WP_BASE_URL", "https://wgc.net.in/hn")

WOO_BASE_URL        = os.getenv("WOO_BASE_URL",        f"{_WP_BASE}/wp-json/wc/v3")
CUSTOM_API_BASE_URL = os.getenv("CUSTOM_API_BASE_URL", f"{_WP_BASE}/wp-json/custom-api/v1")

WOO_CONSUMER_KEY    = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
REQUEST_TIMEOUT     = 30

# ── Dev cache ─────────────────────────────────────────────────────────────────

DEV_CACHE_ENABLED        = os.getenv("DEV_CACHE",        "false").lower() == "true"
UPDATE_DEV_CACHE_ENABLED = os.getenv("UPDATE_DEV_CACHE", "false").lower() == "true"

DEV_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".dev_cache"
)

# ── Data files ────────────────────────────────────────────────────────────────

DATA_FOLDER = os.getenv("DATA_FOLDER", "data")
DATA_DIR    = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DATA_FOLDER
)

FILE_MAP = {
    "attributes": "all-attributes-and-terms.json",
    "tags":       "list-of-all-tags.json",
    "categories": "product-category.json",
    "products":   "product-list.json",
}

VECTOR_CACHE_FILE = os.path.join(DEV_CACHE_DIR, "semantic_vectors.pt")

# ── HTTP ──────────────────────────────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# ── Currency ──────────────────────────────────────────────────────────────────

CURRENCY_MAP = {
    "USD": "$",   "EUR": "€",   "GBP": "£",   "INR": "₹",
    "JPY": "¥",   "CNY": "¥",   "AUD": "A$",  "CAD": "C$",
    "CHF": "CHF", "SEK": "kr",  "NOK": "kr",  "DKK": "kr",
    "NZD": "NZ$", "SGD": "S$",  "HKD": "HK$", "KRW": "₩",
    "TRY": "₺",   "BRL": "R$",  "ZAR": "R",   "MXN": "MX$",
    "MYR": "RM",  "THB": "฿",   "PHP": "₱",   "IDR": "Rp",
    "AED": "د.إ", "SAR": "﷼",  "PLN": "zł",  "CZK": "Kč",
    "HUF": "Ft",  "RUB": "₽",   "ILS": "₪",   "CLP": "CL$",
    "COP": "COL$","PEN": "S/.", "ARS": "AR$", "TWD": "NT$",
    "VND": "₫",   "PKR": "₨",   "BDT": "৳",   "LKR": "Rs",
    "NGN": "₦",   "KES": "KSh", "EGP": "E£",  "UAH": "₴",
    "RON": "lei", "BGN": "лв",  "HRK": "kn",  "ISK": "kr",
}

# ── Platform selection ────────────────────────────────────────────────────────

ECOMMERCE_BACKEND    = os.getenv("ECOMMERCE_BACKEND",    "woocommerce").lower()
SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN", "")

# ── Shopify OAuth (client credentials flow) ───────────────────────────────────
# These never change — store them in .env and leave them there.
# The access token is managed automatically; do NOT set SHOPIFY_ADMIN_TOKEN manually.

SHOPIFY_CLIENT_ID     = os.getenv("SHOPIFY_CLIENT_ID",     "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")

# SHOPIFY_ADMIN_TOKEN is kept for local dev / one-off overrides only.
# When SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET are present, the token
# manager takes over and this value is ignored at runtime.
SHOPIFY_ADMIN_TOKEN  = os.getenv("SHOPIFY_ADMIN_TOKEN",  "")