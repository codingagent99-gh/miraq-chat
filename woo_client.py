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
        import time
        
        params = dict(api_call.params)
        
        # Only add auth params for standard WooCommerce API, not for custom API
        is_custom_api = "/custom-api/" in api_call.endpoint or api_call.is_custom_api
        if not is_custom_api:
            params["consumer_key"] = WOO_CONSUMER_KEY
            params["consumer_secret"] = WOO_CONSUMER_SECRET

        # Prepare params for logging (exclude sensitive data)
        log_params = {k: v for k, v in params.items() if k not in ("consumer_key", "consumer_secret")}
        
        # Log API request with params, description, and custom_api flag
        sanitized_endpoint = sanitize_url(api_call.endpoint)
        logger.info(
            f"WooCommerce API request: {api_call.method} {sanitized_endpoint} | "
            f"params={log_params} | description=\"{api_call.description}\" | "
            f"custom_api={is_custom_api}"
        )

        # Track start time for response time calculation
        start_time = time.time()
        
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
            
            # Calculate response time
            response_time_ms = int((time.time() - start_time) * 1000)
            
            resp.raise_for_status()
            
            # Parse response data
            data = resp.json()
            
            # Count results properly
            if isinstance(data, list):
                result_count = len(data)
            elif isinstance(data, dict):
                if "products" in data:
                    result_count = len(data["products"])
                elif "id" in data:
                    result_count = 1
                else:
                    result_count = "N/A"
            else:
                result_count = "N/A"
            
            # Extract headers
            total = resp.headers.get("X-WP-Total", "N/A")
            total_pages = resp.headers.get("X-WP-TotalPages", "N/A")
            
            # Log API response with details
            logger.info(
                f"WooCommerce API response: {api_call.method} {sanitized_endpoint} | "
                f"status={resp.status_code} | results={result_count} | "
                f"total={total} | total_pages={total_pages} | "
                f"response_time_ms={response_time_ms}"
            )
            
            return {
                "success": True,
                "data": data,
                "total": total,
                "total_pages": total_pages,
            }
        except Exception as e:
            # Calculate response time even on error
            response_time_ms = int((time.time() - start_time) * 1000)
            
            logger.error(
                f"WooCommerce API error: {api_call.method} {sanitized_endpoint} | "
                f"error={str(e)} | response_time_ms={response_time_ms}",
                exc_info=True
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
