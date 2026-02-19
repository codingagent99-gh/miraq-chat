"""
WooCommerce API client for executing API calls.
"""

from typing import List
import requests as http_requests

from models import WooAPICall
from app_config import WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET, BROWSER_HEADERS
from chat_logger import get_logger, sanitize_url

logger = get_logger("miraq_chat")


class WooClient:
    """Executes WooCommerce API calls with browser UA + query-string auth."""

    def __init__(self):
        self.session = http_requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def execute(self, api_call: WooAPICall) -> dict:
        """Execute a single API call and return raw response."""
        params = dict(api_call.params)
        
        # Only add auth params for standard WooCommerce API, not for custom API
        is_custom_api = "/custom-api/" in api_call.endpoint
        if not is_custom_api:
            params["consumer_key"] = WOO_CONSUMER_KEY
            params["consumer_secret"] = WOO_CONSUMER_SECRET

        # Log API call (sanitize sensitive data)
        sanitized_endpoint = sanitize_url(api_call.endpoint)
        logger.info(f"WooCommerce API call: {api_call.method} {sanitized_endpoint}")

        try:
            if api_call.method == "GET":
                resp = self.session.get(
                    api_call.endpoint,
                    params=params,
                    timeout=30,
                )
            else:
                # For non-GET methods, only add auth if not custom API
                auth_params = {} if is_custom_api else {
                    "consumer_key": WOO_CONSUMER_KEY,
                    "consumer_secret": WOO_CONSUMER_SECRET,
                }
                resp = self.session.request(
                    method=api_call.method,
                    url=api_call.endpoint,
                    params=auth_params,
                    json=api_call.body,
                    timeout=30,
                )
            resp.raise_for_status()
            logger.info(f"WooCommerce API response: status={resp.status_code}, success=True")
            return {
                "success": True,
                "data": resp.json(),
                "total": resp.headers.get("X-WP-Total"),
                "total_pages": resp.headers.get("X-WP-TotalPages"),
            }
        except Exception as e:
            logger.error(f"WooCommerce API error: {api_call.method} {sanitized_endpoint} | error={str(e)}", exc_info=True)
            return {"success": False, "data": [], "error": str(e)}

    def execute_all(self, api_calls: List[WooAPICall]) -> List[dict]:
        results = []
        for call in api_calls:
            result = self.execute(call)
            results.append(result)
        return results


# Global WooClient instance
woo_client = WooClient()
