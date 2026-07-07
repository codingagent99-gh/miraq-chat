"""
store_loader/fetcher.py — Handles all data fetching: live WooCommerce API,
local JSON files, and dev cache persistence.
"""

import os
import json
import time
from typing import List, Dict, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from chat_logger import get_logger
from store_loader.config import (
    DATA_DIR, FILE_MAP, DEV_CACHE_DIR,
    CURRENCY_MAP,
)

logger = get_logger("miraq_chat")



_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "application/json",
}

def _custom_api_headers(consumer_key: str, consumer_secret: str) -> dict:
    """
    Headers for the custom-api/v1/* endpoints.
    WC_Chat_Security.validate_request() reads credentials from
    X-Consumer-Key / X-Consumer-Secret, NOT from Basic Auth.
    """
    return {
        **_API_HEADERS,
        "X-Consumer-Key":    consumer_key,
        "X-Consumer-Secret": consumer_secret,
    }


# ══════════════════════════════════════════════════════════════
# LOCAL FILE I/O
# ══════════════════════════════════════════════════════════════

def read_json(filename: str) -> Optional[List]:
    """Read a JSON file from the data directory."""
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        logger.warning(f"StoreLoader: File not found: {filename}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_from_local_files() -> dict:
    """
    Load all store data from local JSON files.
    Returns a dict with keys: categories, tags, products, all_attributes_raw, attribute_terms, currency_symbol.
    """
    logger.info(f"StoreLoader: 📁 Loading local data from {DATA_DIR}")

    categories         = read_json(FILE_MAP["categories"]) or []
    tags               = read_json(FILE_MAP["tags"]) or []
    products           = read_json(FILE_MAP["products"]) or []
    all_attributes_raw = read_json(FILE_MAP["attributes"]) or []

    attribute_terms = {
        int(attr["attribute_id"]): attr.get("terms", [])
        for attr in all_attributes_raw
        if attr.get("attribute_id")
    }

    return {
        "categories":         categories,
        "tags":               tags,
        "products":           products,
        "all_attributes_raw": all_attributes_raw,
        "attribute_terms":    attribute_terms,
        "currency_symbol":    "₹",   # Force local testing to INR
        "expected_product_count": None,
    }


def save_to_local_files(categories, tags, all_attributes_raw, products):
    """Save live API data to local JSON cache files."""
    os.makedirs(DATA_DIR, exist_ok=True)
    files_to_save = {
        FILE_MAP["categories"]: categories,
        FILE_MAP["tags"]:       tags,
        FILE_MAP["attributes"]: all_attributes_raw,
        FILE_MAP["products"]:   products,
    }
    for filename, data in files_to_save.items():
        path = os.path.join(DATA_DIR, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"StoreLoader: 💾 Saved {len(data)} items → {filename}")
        except Exception as e:
            logger.error(f"StoreLoader: Failed to save {filename}: {e}")


def dump_lookups_for_debugging(loader):
    """Dump processed lookup dictionaries to a file in dev mode."""
    dump_path = os.path.join(DEV_CACHE_DIR, "lookups_debug.json")
    try:
        dump_data = {
            "store_generic_terms":    list(loader._store_generic_terms) if loader._store_generic_terms else [],
            "attribute_by_id":        loader.attribute_by_id,
            "category_by_id":         loader.category_by_id,
            "category_by_name_lower": loader.category_by_name_lower,
            "category_keywords":      loader.category_keywords,
            "tag_by_id":              loader.tag_by_id,
            "tag_by_name_lower":      loader.tag_by_name_lower,
            "product_by_name_lower":  loader.product_by_name_lower,
            "product_name_tokens":    loader.product_name_tokens,
        }
        os.makedirs(DEV_CACHE_DIR, exist_ok=True)
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(dump_data, f, indent=2)
        logger.info(f"StoreLoader: Dumped lookup dictionaries to {dump_path}")
    except Exception as e:
        logger.error(f"StoreLoader: Failed to dump lookups: {e}")


# ══════════════════════════════════════════════════════════════
# RATE-LIMIT HELPERS
# ══════════════════════════════════════════════════════════════

def _wait_for_retry(resp, attempt: int, url: str):
    """
    Sleep before retrying a failed request.
    WordPress.com Atomic never sends Retry-After, so we use a fixed back-off.
    """
    if resp is not None and resp.status_code == 429:
        wait = 30 * (attempt + 1)   # 30s, 60s, 90s
        logger.warning(
            f"StoreLoader: 429 on {url} — waiting {wait}s (attempt {attempt + 1})"
        )
        time.sleep(wait)
    else:
        wait = 2 ** attempt
        time.sleep(wait)


# ══════════════════════════════════════════════════════════════
# LIVE API FETCHING
# ══════════════════════════════════════════════════════════════

def fetch_all_pages(session, url: str, consumer_key: str, consumer_secret: str,
                    extra_params: Dict = None, timeout: int = 30,
                    max_retries: int = 3) -> List[Dict]:
    """Fetch all pages from a paginated WooCommerce REST endpoint."""
    auth      = HTTPBasicAuth(consumer_key, consumer_secret)
    all_items = []
    page      = 1

    while True:
        params = {"per_page": 100, "page": page}
        if extra_params:
            params.update(extra_params)

        data = None
        resp = None
        for attempt in range(max_retries):
            try:
                resp = session.get(url, auth=auth, headers=_API_HEADERS,
                                   params=params, timeout=timeout)
                if page == 1:
                    logger.debug(f"RAW RESPONSE [{resp.status_code}]: {resp.text[:500]}")
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt < max_retries - 1:
                    _wait_for_retry(resp, attempt, url)
                else:
                    logger.error(f"StoreLoader: All retries failed for {url} page {page}")
                    return all_items

        if not data:
            break
        all_items.extend(data)
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1)) if resp else 1
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.5)

    return all_items


def fetch_all_pages_with_total(session, url: str, consumer_key: str, consumer_secret: str,
                               extra_params: Dict = None, timeout: int = 30,
                               max_retries: int = 3) -> Tuple[List[Dict], Optional[int]]:
    """Fetch all pages and return (items, expected_total)."""
    auth           = HTTPBasicAuth(consumer_key, consumer_secret)
    all_items      = []
    page           = 1
    expected_total = None

    while True:
        params = {"per_page": 100, "page": page}
        if extra_params:
            params.update(extra_params)

        data = None
        resp = None
        for attempt in range(max_retries):
            try:
                resp = session.get(url, auth=auth, headers=_API_HEADERS,
                                   params=params, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt < max_retries - 1:
                    _wait_for_retry(resp, attempt, url)
                else:
                    return all_items, expected_total

        if not data:
            break
        all_items.extend(data)

        if resp:
            if expected_total is None:
                try:
                    expected_total = int(resp.headers.get("X-WP-Total", 0))
                except Exception:
                    pass
            total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages:
                break
        else:
            break
        page += 1
        time.sleep(0.5)

    return all_items, expected_total


def fetch_currency_symbol(session, base_url: str, consumer_key: str,
                          consumer_secret: str, timeout: int = 30) -> str:
    """Fetch the active currency symbol from WooCommerce."""
    logger.info("StoreLoader: Fetching store currency...")
    try:
        url  = f"{base_url}/data/currencies/current"
        auth = HTTPBasicAuth(consumer_key, consumer_secret)
        resp = session.get(url, auth=auth, headers=_API_HEADERS, timeout=timeout)
        resp.raise_for_status()
        data   = resp.json()
        symbol = data.get("symbol")
        if symbol:
            return symbol
        code = data.get("code", "USD")
        return CURRENCY_MAP.get(code.upper(), "$")
    except Exception as e:
        logger.warning(f"StoreLoader: Failed to fetch currency, defaulting to $. Error: {e}")
        return "$"


def load_from_live_api(session, base_url: str, custom_api_base: str,
                       consumer_key: str, consumer_secret: str,
                       timeout: int = 30) -> dict:
    """
    Fetch all store data from live WooCommerce API.
    Returns same dict shape as load_from_local_files().
    """
    logger.info("StoreLoader: 🌐 Fetching data from live WooCommerce API...")

    currency_symbol = fetch_currency_symbol(
        session, base_url, consumer_key, consumer_secret, timeout
    )

    # Attributes — custom endpoint, auth via HTTPBasicAuth
    custom_attr_url = f"{custom_api_base}/all-attributes"
    logger.info(f"StoreLoader: Fetching attributes from {custom_attr_url}")
    try:
        resp = session.get(
            custom_attr_url,
            headers=_custom_api_headers(consumer_key, consumer_secret),
            timeout=timeout,
        )
        resp.raise_for_status()
        all_attributes_raw = resp.json()
        attribute_terms = {
            int(attr["attribute_id"]): attr.get("terms", [])
            for attr in all_attributes_raw
            if attr.get("attribute_id")
        }
    except Exception as e:
        logger.error(f"StoreLoader: Failed to fetch attributes: {e}")
        all_attributes_raw = []
        attribute_terms    = {}

    # Categories
    logger.info("StoreLoader: Fetching categories...")
    categories = fetch_all_pages(
        session, f"{base_url}/products/categories",
        consumer_key, consumer_secret, {"hide_empty": True}, timeout,
    )

    # Tags
    logger.info("StoreLoader: Fetching tags...")
    tags = fetch_all_pages(
        session, f"{base_url}/products/tags",
        consumer_key, consumer_secret, {"hide_empty": True}, timeout,
    )

    # Products
    logger.info("StoreLoader: Fetching products...")
    products, expected_product_count = fetch_all_pages_with_total(
        session, f"{base_url}/products", consumer_key, consumer_secret,
        {"status": "publish", "per_page": 100}, timeout,
    )

    return {
        "categories":         categories,
        "tags":               tags,
        "products":           products,
        "all_attributes_raw": all_attributes_raw,
        "attribute_terms":    attribute_terms,
        "currency_symbol":    currency_symbol,
        "expected_product_count": expected_product_count,
    }