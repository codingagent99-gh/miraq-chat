"""
HTTP client for runtime WooCommerce API calls.
Uses same browser UA + query-string auth that passed Test 3.
"""

import os
import requests
from typing import List
from dotenv import load_dotenv
from models import WooAPICall

load_dotenv()

WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
REQUEST_TIMEOUT = 30

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class WooCommerceClient:
    def __init__(self):
        self.consumer_key = WOO_CONSUMER_KEY
        self.consumer_secret = WOO_CONSUMER_SECRET
        self.timeout = REQUEST_TIMEOUT

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def execute(self, api_call: WooAPICall) -> dict:
        """Execute a single WooCommerce API call."""
        try:
            # Inject auth into params (query-string method)
            params = dict(api_call.params)
            params["consumer_key"] = self.consumer_key
            params["consumer_secret"] = self.consumer_secret

            if api_call.method == "GET":
                response = self.session.get(
                    api_call.endpoint,
                    params=params,
                    timeout=self.timeout,
                )
            else:
                response = self.session.request(
                    method=api_call.method,
                    url=api_call.endpoint,
                    params={
                        "consumer_key": self.consumer_key,
                        "consumer_secret": self.consumer_secret,
                    },
                    json=api_call.body,
                    timeout=self.timeout,
                )

            response.raise_for_status()
            return {
                "success": True,
                "data": response.json(),
                "status_code": response.status_code,
                "total": response.headers.get("X-WP-Total"),
                "total_pages": response.headers.get("X-WP-TotalPages"),
            }
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": str(e),
                "status_code": getattr(e.response, "status_code", None) if hasattr(e, "response") else None,
            }

    def execute_all(self, api_calls: List[WooAPICall]) -> List[dict]:
        """Execute multiple API calls sequentially."""
        results = []
        for call in api_calls:
            result = self.execute(call)
            results.append({
                "description": call.description,
                "method": call.method,
                "endpoint": call.endpoint,
                "response": result,
            })
        return results