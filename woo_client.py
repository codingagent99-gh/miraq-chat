"""
WooCommerce API client for executing API calls.
"""

from typing import List
import requests as http_requests

from models import WooAPICall
from app_config import (
    WOO_CONSUMER_KEY,
    WOO_CONSUMER_SECRET,
    BROWSER_HEADERS,
    WOO_BASE_URL,
    CUSTOM_API_BASE_URL,
)
from chat_logger import get_logger, get_api_logger, sanitize_url

logger = get_logger("miraq_chat")
api_logger = get_api_logger()


class WooClient:
    """Executes WooCommerce API calls with browser UA + query-string auth."""

    def __init__(self):
        self.session = http_requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def execute(self, api_call: WooAPICall) -> dict:
        """Execute a single API call and return raw response."""
        from ecommerce.woo_adapters import normalize_response

        params = dict(api_call.params)
        base_url = CUSTOM_API_BASE_URL if api_call.surface == "custom_plugin" else WOO_BASE_URL
        endpoint_url = api_call.endpoint if api_call.endpoint.startswith("http") else f"{base_url.rstrip('/')}/{api_call.endpoint.lstrip('/')}"

        if api_call.surface != "custom_plugin":
            params["consumer_key"] = WOO_CONSUMER_KEY
            params["consumer_secret"] = WOO_CONSUMER_SECRET

        import json as _json
        import time as _time

        # Sanitize endpoint for logging
        sanitized_endpoint = sanitize_url(endpoint_url)
        endpoint_short = sanitized_endpoint.split("/")[-1]  # e.g. "products-advanced-new"
        safe_params = {k: v for k, v in params.items() if k not in ("consumer_key", "consumer_secret")}

        # ── REQUEST: log full details to api.txt ──────────────────────────────
        context = ""
        if api_call.session_id or api_call.user_message:
            context = f" | session={api_call.session_id} | q={api_call.user_message!r}"

        if api_call.method == "GET":
            api_logger.info(
                f"REQUEST {api_call.method} {sanitized_endpoint} | params={safe_params}{context}"
            )
        else:
            try:
                body_str = _json.dumps(api_call.body, separators=(",", ":"))
            except Exception:
                body_str = str(api_call.body)
            api_logger.info(
                f"REQUEST {api_call.method} {sanitized_endpoint} | body={body_str}{context}"
            )

        # Also keep a brief line in the main chat log
        logger.info(f"API call: {api_call.method} {endpoint_short} | params={safe_params}")

        _req_start = _time.time()

        try:
            if api_call.method == "GET":
                custom_headers = {
                    "X-Consumer-Key":    WOO_CONSUMER_KEY,
                    "X-Consumer-Secret": WOO_CONSUMER_SECRET,
                } if api_call.surface == "custom_plugin" else {}

                resp = self.session.get(
                    endpoint_url,
                    params=params,
                    headers=custom_headers,
                    timeout=45,
                )
            else:
                auth_params = {} if api_call.surface == "custom_plugin" else {
                    "consumer_key": WOO_CONSUMER_KEY,
                    "consumer_secret": WOO_CONSUMER_SECRET,
                }
                custom_headers = {
                    "X-Consumer-Key":    WOO_CONSUMER_KEY,
                    "X-Consumer-Secret": WOO_CONSUMER_SECRET,
                } if api_call.surface == "custom_plugin" else {}

                resp = self.session.request(
                    method=api_call.method,
                    url=endpoint_url,
                    params=auth_params,
                    json=api_call.body,
                    headers=custom_headers,
                    timeout=45,
                )

            resp.raise_for_status()
            _elapsed_ms = round((_time.time() - _req_start) * 1000)
            data = resp.json()

            # ── RESPONSE: log summary to api.txt ─────────────────────────────
            # Extract product ids+names without loading the full body into the log
            if isinstance(data, dict) and "products" in data:
                items = data.get("products", [])
            elif isinstance(data, list):
                items = data
            else:
                items = []

            if items:
                product_summary = ", ".join(
                    f"{p.get('id')}:{p.get('name', '?')}"
                    for p in items[:20]  # cap at 20 to avoid huge lines
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

            # Brief line in main chat log
            logger.info(
                f"API response: {endpoint_short} | status={resp.status_code} | "
                f"count={len(items)} | time_ms={_elapsed_ms}"
            )

            normalized_data, total, total_pages = normalize_response(api_call.operation, data, resp.headers)

            return {
                "success": True,
                "data": normalized_data,
                "total": total,
                "total_pages": total_pages,
            }

        except Exception as e:
            # Capture response body for HTTP errors to aid debugging
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
            result = self.execute(call)
            results.append(result)
        return results


# Global WooClient instance
woo_client = WooClient()
