"""
WooCommerce API client for executing API calls.
"""

from typing import List
import requests as http_requests
from requests.auth import HTTPBasicAuth

from models import WooAPICall
from app_config import (
    WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET, WOO_BASE_URL, CUSTOM_API_BASE_URL,
    ECOMMERCE_BACKEND,
)
from chat_logger import get_logger, get_api_logger, sanitize_url

logger = get_logger("miraq_chat")
api_logger = get_api_logger()

# Minimal headers that pass WordPress.com Atomic's bot detection.
# Full BROWSER_HEADERS with query-string credentials triggered 429s.
_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept":     "application/json",
}

# Standard WooCommerce REST auth (Basic Auth)
_WC_AUTH = HTTPBasicAuth(WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET)

# Custom plugin endpoints read credentials from these headers
# (see WC_Chat_Security::validate_request in class-security.php)
_CUSTOM_API_HEADERS = {
    **_BASE_HEADERS,
    "X-Consumer-Key":    WOO_CONSUMER_KEY,
    "X-Consumer-Secret": WOO_CONSUMER_SECRET,
}


class WooClient:
    """Executes WooCommerce API calls."""

    def __init__(self):
        self.session = http_requests.Session()
        self.session.headers.update(_BASE_HEADERS)

    def execute(self, api_call: WooAPICall) -> dict:
        """Execute a single API call and return raw response."""
        import json as _json
        import time as _time

        # ── Shopify backstop ──────────────────────────────────────────────────
        # On a Shopify deployment no WooCommerce request is ever legitimate.
        # ShopifyEndpoints returns surface="shopify_admin" stubs whose endpoint
        # paths are placeholders; executing them would resolve against
        # WOO_BASE_URL and hit an unrelated store.
        #
        # This guard lives here (not only in chat.py's dispatcher) because
        # ~25 call sites across handlers, parsers and routes call woo_client
        # directly, bypassing that dispatcher. execute_all() delegates here
        # too, so this is the one place that provably covers all of them.
        #
        # Returning the standard failure envelope — rather than raising —
        # means every existing caller's `if result.get("success")` branch
        # degrades safely with no other change.
        if ECOMMERCE_BACKEND == "shopify":
            logger.warning(
                "WooClient: blocked WooCommerce call on Shopify deployment | "
                f"{api_call.method} {api_call.endpoint} | "
                f"surface={getattr(api_call, 'surface', '')} | "
                f"description={api_call.description!r}"
            )
            return {
                "success": False,
                "data": [],
                "error": "unsupported_on_shopify",
                "unsupported_on_shopify": True,
            }

        params = dict(api_call.params)
        is_custom_api = api_call.surface == "custom_plugin"

        # Resolve relative endpoints to full URLs
        endpoint = api_call.endpoint
        if not endpoint.startswith("http"):
            base = CUSTOM_API_BASE_URL if is_custom_api else WOO_BASE_URL
            endpoint = base.rstrip("/") + endpoint

        # Auth strategy:
        #   - custom-api/v1/*  → X-Consumer-Key / X-Consumer-Secret headers
        #   - wc/v3/*          → HTTPBasicAuth (no credentials in query string)
        auth    = None       if is_custom_api else _WC_AUTH
        headers = _CUSTOM_API_HEADERS if is_custom_api else {}

        # ── Logging ───────────────────────────────────────────────────────────
        sanitized_endpoint = sanitize_url(endpoint)
        endpoint_short     = sanitized_endpoint.split("/")[-1]
        safe_params        = {k: v for k, v in params.items()
                              if k not in ("consumer_key", "consumer_secret")}

        context = ""
        if api_call.session_id or api_call.user_message:
            context = f" | session={api_call.session_id} | q={api_call.user_message!r}"

        if api_call.method == "GET":
            api_logger.info(f"REQUEST GET {sanitized_endpoint} | params={safe_params}{context}")
        else:
            try:
                body_str = _json.dumps(api_call.body, separators=(",", ":"))
            except Exception:
                body_str = str(api_call.body)
            api_logger.info(f"REQUEST {api_call.method} {sanitized_endpoint} | body={body_str}{context}")

        logger.info(f"API call: {api_call.method} {endpoint_short} | params={safe_params}")

        _req_start = _time.time()

        try:
            if api_call.method == "GET":
                resp = self.session.get(
                    endpoint,
                    auth=auth,
                    headers=headers,
                    params=params,
                    timeout=45,
                )
            else:
                resp = self.session.request(
                    method=api_call.method,
                    url=endpoint,
                    auth=auth,
                    headers=headers,
                    params=params,
                    json=api_call.body,
                    timeout=45,
                )

            resp.raise_for_status()
            _elapsed_ms = round((_time.time() - _req_start) * 1000)
            data = resp.json()

            # ── Response logging ──────────────────────────────────────────────
            if isinstance(data, dict) and "products" in data:
                items = data.get("products", [])
            elif isinstance(data, list):
                items = data
            else:
                items = []

            if items:
                product_summary = ", ".join(
                    f"{p.get('id')}:{p.get('name', '?')}" for p in items[:20]
                )
                api_logger.info(
                    f"RESPONSE {api_call.method} {endpoint_short} | "
                    f"status={resp.status_code} | count={len(items)} | "
                    f"time_ms={_elapsed_ms} | products=[{product_summary}]"
                )
            else:
                api_logger.info(
                    f"RESPONSE {api_call.method} {endpoint_short} | "
                    f"status={resp.status_code} | count={len(items)} | "
                    f"time_ms={_elapsed_ms}"
                )

            logger.info(
                f"API response: {endpoint_short} | status={resp.status_code} | "
                f"count={len(items)} | time_ms={_elapsed_ms}"
            )

            if isinstance(data, dict) and "products" in data:
                result = {
                    "success":     True,
                    "data":        data.get("products", []),
                    "total":       str(data.get("total", "")) or None,
                    "total_pages": str(data.get("pages", "")) or None,
                }
                if data.get("or_group_breakdown"):
                    result["or_group_breakdown"] = data["or_group_breakdown"]
                return result

            return {
                "success":     True,
                "data":        data,
                "total":       resp.headers.get("X-WP-Total"),
                "total_pages": resp.headers.get("X-WP-TotalPages"),
            }

        except Exception as e:
            body_preview = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    body_preview = f" | response_body={e.response.text[:500]!r}"
                except Exception:
                    pass
            _elapsed_ms = round((_time.time() - _req_start) * 1000)
            api_logger.error(
                f"RESPONSE {api_call.method} {endpoint_short} | "
                f"status=ERROR | time_ms={_elapsed_ms} | "
                f"error={str(e)}{body_preview}"
            )
            logger.error(
                f"API error: {endpoint_short} | error={str(e)}{body_preview}",
                exc_info=True,
            )
            return {"success": False, "data": [], "error": str(e)}

    def execute_all(self, api_calls: List[WooAPICall]) -> List[dict]:
        results = []
        for call in api_calls:
            results.append(self.execute(call))
        return results


# Global WooClient instance
woo_client = WooClient()