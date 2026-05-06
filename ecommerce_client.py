"""
ecommerce_client.py — Abstract e-commerce client interface.

Defines EcommerceClient, the backend-neutral contract that all e-commerce
integrations must satisfy, and a get_ecommerce_client() factory that returns
the active implementation based on the ECOMMERCE_BACKEND env var.

Current implementations
-----------------------
- ``woocommerce`` (default): WooClient in woo_client.py

Adding a new backend (e.g. Shopify)
-------------------------------------
1. Create ``shopify_client.py`` with ``ShopifyClient(EcommerceClient)``.
2. Add a branch to get_ecommerce_client():

       elif backend == "shopify":
           from shopify_client import ShopifyClient
           return ShopifyClient()

3. Set ``ECOMMERCE_BACKEND=shopify`` in the environment.
"""

import os
from abc import ABC, abstractmethod
from typing import Any, List


class EcommerceClient(ABC):
    """
    Backend-neutral interface for all e-commerce API interactions.

    Callers build an APICall descriptor (currently WooAPICall) via
    api_builder, then hand it to execute() / execute_all().  Return values
    are plain dicts so concrete implementations are free to normalise
    backend-specific responses into the same shape.
    """

    @abstractmethod
    def execute(self, api_call: Any) -> dict:
        """
        Execute a single API call and return a response dict.

        Expected return shape::

            {
                "success": bool,
                "data":    list | dict,   # normalised payload
                "total":   str | None,    # total record count (if paginated)
                "total_pages": str | None,
                "error":   str | None,    # present only when success is False
            }
        """

    @abstractmethod
    def execute_all(self, api_calls: List[Any]) -> List[dict]:
        """
        Execute a list of API calls in order and return their responses.

        Returns a list of response dicts in the same order as api_calls,
        each matching the shape described in execute().
        """


def get_ecommerce_client() -> EcommerceClient:
    """
    Factory that returns the active e-commerce client.

    Reads the ``ECOMMERCE_BACKEND`` environment variable (default:
    ``"woocommerce"``).  Add a new ``elif`` branch here when a second
    backend is implemented.
    """
    backend = os.getenv("ECOMMERCE_BACKEND", "woocommerce").lower()

    if backend == "woocommerce":
        from woo_client import WooClient  # local import avoids circular dep
        return WooClient()

    raise ValueError(
        f"Unknown ECOMMERCE_BACKEND value: {backend!r}.  "
        "Supported values: 'woocommerce'."
    )
