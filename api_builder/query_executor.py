"""
api_builder/query_executor.py — Backend-neutral query executor.

A QueryExecutor takes a query tree (list of nodes from `query_tree.py`) plus
pagination params and returns a normalized response:

    {
        "products": [neutral_product_dict, ...],
        "page":     int,
        "per_page": int,
        "total":    int,
        "pages":    int,
    }

Each backend implements its own executor:

    - WooQueryExecutor      → POSTs to the custom WP plugin's
                              /products-advanced-new endpoint.
    - ShopifyQueryExecutor  → in-memory eval over store_loader catalog
                              with optional GraphQL pushdown (Phase 5b).

Callers depend on this Protocol, not on Woo specifics.
"""

from typing import List, Optional, Protocol

from chat_logger import get_logger
from api_builder.query_tree import serialize_query

logger = get_logger("miraq_chat")


# ─── Protocol ────────────────────────────────────────────────────────────────

class QueryExecutor(Protocol):
    """Backend-agnostic query executor interface."""

    def execute(
        self,
        conditions: list,
        page: int,
        per_page: int,
        *,
        product_id: Optional[int] = None,
        search_term: Optional[str] = None,
    ) -> dict:
        """Run the query tree against the backend and return a normalized
        product page response."""
        ...


# ─── Woo implementation ──────────────────────────────────────���───────────────

class WooQueryExecutor:
    """
    Executes a query tree against the custom WooCommerce plugin endpoint
    (POST /products-advanced-new).

    Wraps the existing pipeline:

        tree → serialize_query → endpoints.products_advanced → woo_client → parse

    so that the rest of the app talks to a uniform interface and Phase 5b
    (Shopify) can drop in alongside.
    """

    def __init__(self, endpoints, client):
        """
        Args:
            endpoints: An EcommerceEndpoints implementation (WooEndpoints).
            client:    The woo_client module (or any object exposing
                       `.execute(WooAPICall) -> dict` with `.get("data")`).
        """
        self._endpoints = endpoints
        self._client = client

    def execute(
        self,
        conditions: list,
        page: int,
        per_page: int,
        *,
        product_id: Optional[int] = None,
        search_term: Optional[str] = None,
    ) -> dict:
        body = serialize_query(conditions, page, per_page)

        # ── Special-case body shaping that historically lived in
        #    build_advanced_filter_call. Kept here so the executor owns
        #    the final on-wire shape for Woo.
        if product_id:
            body["ids"] = [product_id]
            body.pop("stock_status", None)
            body.pop("filters", None)
        elif search_term and not body.get("filters"):
            # No taxonomy filters — caller should have used woo's text
            # search instead. Log and continue (matches legacy behaviour).
            logger.warning(
                f"WooQueryExecutor: search_term='{search_term}' ignored AND no "
                "taxonomy conditions present — query will return arbitrary products."
            )

        api_call = self._endpoints.products_advanced(body=body)
        raw = self._client.execute(api_call)
        data = raw.get("data") or {}

        return {
            "products": [
                self._endpoints.parse_product(p) for p in data.get("products", [])
            ],
            "page":     int(data.get("page", page)),
            "per_page": int(data.get("per_page", per_page)),
            "total":    int(data.get("total", 0)),
            "pages":    int(data.get("pages", 0)),
            "_raw":     data,
        }