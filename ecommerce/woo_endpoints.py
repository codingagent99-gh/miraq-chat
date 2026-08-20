"""
ecommerce/woo_endpoints.py — WooCommerce concrete implementation of EcommerceEndpoints.

Every WooCommerce URL string lives in this file and nowhere else.
Callers construct API calls via the methods below; ``woo_client.execute()``
handles auth and transport.

Relative paths are used throughout (e.g. ``/orders`` rather than the full URL).
``woo_client.execute()`` prepends the appropriate base URL based on the
``surface`` field:
  - ``"admin"``        → ``WOO_BASE_URL``       (e.g. {WP_BASE}/wp-json/wc/v3)
  - ``"custom_plugin"``→ ``CUSTOM_API_BASE_URL`` (e.g. {WP_BASE}/wp-json/custom-api/v1)

Each ``fetch_*`` / ``list_*`` call constructor is paired with a ``parse_*`` method
that normalizes the raw WooCommerce response dict into a backend-neutral shape.
All parsers include a ``_raw`` key with the original response so callers can
access any Woo-specific field that has not yet been normalized.
"""

import re
from typing import Dict, List, Optional, Union

from chat_logger import get_logger
from models import WooAPICall
from woo_client import woo_client

logger = get_logger("miraq_chat")


# ── Address normalization helper ────────────────────────────────────────────

def _normalize_woo_address(addr: dict) -> dict:
    """Normalize a WooCommerce address sub-dict to the backend-neutral shape.

    WooCommerce address dicts already use the neutral key names
    (``address_1``, ``postcode``, etc.).  A future Shopify parser will remap
    ``address1`` → ``address_1``, ``zip`` → ``postcode``, etc. before calling
    a similar helper so callers always receive the same shape.

    Returns:
        {
            "address_1": str,
            "address_2": str,
            "city": str,
            "state": str,
            "postcode": str,
            "country": str,
        }
    """
    return {
        "address_1": addr.get("address_1", ""),
        "address_2": addr.get("address_2", ""),
        "city": addr.get("city", ""),
        "state": addr.get("state", ""),
        "postcode": addr.get("postcode", ""),
        "country": addr.get("country", ""),
    }


class WooEndpoints:
    """WooCommerce implementation of EcommerceEndpoints."""

    # ── Store metadata ──────────────────────────────────────────────────────

    def fetch_currency(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.1 — GET /data/currencies/current"""
        return WooAPICall(
            method="GET",
            endpoint="/data/currencies/current",
            params={},
            surface="admin",
            description=description or "Fetch active currency",
            requires_resolution=requires_resolution or [],
        )

    def list_attributes(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.2 — GET /all-attributes (custom_plugin surface)"""
        return WooAPICall(
            method="GET",
            endpoint="/all-attributes",
            params={},
            surface="custom_plugin",
            description=description or "Fetch all product attributes and terms",
            requires_resolution=requires_resolution or [],
        )

    def fetch_checkout_fields(
        self,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """GET /checkout-fields — WC checkout field definitions (custom_plugin surface).

        Returns the checkout fields grouped by "billing" / "shipping" / "order",
        each field carrying label / required / type / priority. Consumed by
        utils/checkout_fields.get_required_fields() to enrich (never shrink) the
        static required-field floor.
        """
        return WooAPICall(
            method="GET",
            endpoint="/checkout-fields",
            params={},
            surface="custom_plugin",
            description=description or "Fetch checkout field definitions",
            requires_resolution=requires_resolution or [],
        )

    def list_categories(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 2.3 — GET /products/categories"""
        params = {
            "per_page": per_page,
            "page": page,
            "hide_empty": True,
            **extra_params,
        }
        return WooAPICall(
            method="GET",
            endpoint="/products/categories",
            params=params,
            surface="admin",
            description=description or "List product categories",
            requires_resolution=requires_resolution or [],
        )

    def list_tags(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 2.4 — GET /products/tags"""
        params = {
            "per_page": per_page,
            "page": page,
            "hide_empty": True,
            **extra_params,
        }
        return WooAPICall(
            method="GET",
            endpoint="/products/tags",
            params=params,
            surface="admin",
            description=description or "List product tags",
            requires_resolution=requires_resolution or [],
        )

    def list_published_products(
        self,
        page: int,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 2.5 — GET /products?status=publish"""
        return WooAPICall(
            method="GET",
            endpoint="/products",
            params={"status": "publish", "per_page": per_page, "page": page},
            surface="admin",
            description=description or "List all published products",
            requires_resolution=requires_resolution or [],
        )

    # ── Products ────────────────────────────────────────────────────────────

    def fetch_product(
        self,
        product_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.1 — GET /products/{product_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/products/{product_id}",
            params={},
            surface="admin",
            description=description or f"Fetch product id={product_id}",
            requires_resolution=requires_resolution or [],
        )

    def fetch_variant(
        self,
        product_id: int,
        variant_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.2 — GET /products/{product_id}/variations/{variant_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/products/{product_id}/variations/{variant_id}",
            params={},
            surface="admin",
            description=description or f"Fetch variant product_id={product_id} variant_id={variant_id}",
            requires_resolution=requires_resolution or [],
        )

    def list_variants(
        self,
        product_id: int,
        page: int = 1,
        per_page: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **extra_params,
    ) -> WooAPICall:
        """Row 4.3 — GET /products/{product_id}/variations"""
        params = {"per_page": per_page, "page": page, **extra_params}
        return WooAPICall(
            method="GET",
            endpoint=f"/products/{product_id}/variations",
            params=params,
            surface="admin",
            description=description or f"List variants for product_id={product_id}",
            requires_resolution=requires_resolution or [],
        )

    def products_advanced(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 4.4 — POST /products-advanced-new (custom_plugin surface)"""
        return WooAPICall(
            method="POST",
            endpoint="/products-advanced-new",
            params={},
            body=body,
            surface="custom_plugin",
            description=description or "Advanced product filter",
            requires_resolution=requires_resolution or [],
        )

    def build_cart_variation_payload(
        self,
        *,
        product_id: int,
        variant_id: Optional[int],
        resolved_attrs: Dict[str, str],
        store_loader,
    ) -> List[Dict[str, str]]:
        """Build Woo variation payload for cart-add actions."""

        def _taxonomy_for(attr_key: str) -> str:
            attr = store_loader.resolve_attribute(attr_key) if store_loader else None
            taxonomy = attr.backend_ref.get("taxonomy") if attr and attr.backend_ref else None
            return taxonomy or f"pa_{str(attr_key).lower().replace(' ', '-')}"

        def _slug_for(attr_key: str, value: str) -> str:
            term = (
                store_loader.resolve_attribute_term(attr_key, value)
                if store_loader else None
            )
            slug = term.backend_ref.get("slug") if term and term.backend_ref else None
            return slug or re.sub(r"[^a-z0-9]+", "", str(value).lower())

        def _resolved_payload() -> List[Dict[str, str]]:
            return [
                {"attribute": _taxonomy_for(attr_key), "value": _slug_for(attr_key, display_value)}
                for attr_key, display_value in (resolved_attrs or {}).items()
            ]

        if not variant_id or not product_id:
            return _resolved_payload()

        try:
            var_resp = woo_client.execute(self.fetch_variant(
                product_id=product_id,
                variant_id=variant_id,
                description=f"Fetch variation {variant_id} for cart payload",
            ))
            if not (var_resp.get("success") and isinstance(var_resp.get("data"), dict)):
                raise ValueError("variation fetch failed")

            var_attrs = var_resp["data"].get("attributes", [])
            fixed: set[str] = set()
            result: List[Dict[str, str]] = []

            if isinstance(var_attrs, list):
                for attr in var_attrs:
                    if not isinstance(attr, dict):
                        continue
                    attr_name = attr.get("name", "")
                    taxonomy = _taxonomy_for(attr_name)
                    option = str(attr.get("option", ""))
                    result.append({"attribute": taxonomy, "value": option})
                    fixed.add(taxonomy)

            for attr_key, display_value in (resolved_attrs or {}).items():
                taxonomy = _taxonomy_for(attr_key)
                if taxonomy in fixed:
                    continue
                result.append({"attribute": taxonomy, "value": _slug_for(attr_key, display_value)})
            return result
        except Exception as exc:
            logger.warning(f"build_cart_variation_payload fallback | error={exc}")
            return _resolved_payload()

    # ── Orders ──────────────────────────────────────────────────────────────

    def list_customer_orders(
        self,
        customer_id,
        page: int,
        per_page: int = 5,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **filters,
    ) -> WooAPICall:
        """Row 5.1 — GET /orders"""
        params = {
            "customer": customer_id,
            "per_page": per_page,
            "page": page,
            "orderby": "date",
            "order": "desc",
            **filters,
        }
        return WooAPICall(
            method="GET",
            endpoint="/orders",
            params=params,
            surface="admin",
            description=description or f"List orders for customer {customer_id}",
            requires_resolution=requires_resolution or [],
        )

    def list_all_orders(
        self,
        page: int,
        per_page: int = 20,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
        **filters,
    ) -> WooAPICall:
        """Row 5.1b — GET /orders with NO customer filter (admin only).

        Identical to list_customer_orders minus the `customer` param.

        Enforcement is Python-only: this call goes straight to `wc/v3` with
        the store's own consumer key/secret (see woo_client.py), which is
        NOT the requesting user's credential and has no per-user capability
        of its own to check. The plugin is never in this call's path, so it
        cannot re-check anything here. `is_order_report_admin(role)` on the
        caller — reading a role out of the request payload — is the only
        gate. Accepted as-is for now; if that role field ever becomes
        spoofable from the client, this is the call it protects.
        """
        params = {
            "per_page": per_page,
            "page": page,
            "orderby": "date",
            "order": "desc",
            **filters,
        }
        return WooAPICall(
            method="GET",
            endpoint="/orders",
            params=params,
            surface="admin",
            description=description or "List all store orders",
            requires_resolution=requires_resolution or [],
        )

    def fetch_order(
        self,
        order_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.2 — GET /orders/{order_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/orders/{order_id}",
            params={},
            surface="admin",
            description=description or f"Fetch order id={order_id}",
            requires_resolution=requires_resolution or [],
        )

    def check_stock(
        self,
        product_ids: List[int],
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.3 — Thin wrapper over products_advanced for stock checking."""
        return self.products_advanced(
            body={"ids": product_ids, "per_page": len(product_ids)},
            description=description or "Check stock status for product IDs",
            requires_resolution=requires_resolution or [],
        )

    def create_order(
        self,
        payload: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.4 — POST /orders"""
        return WooAPICall(
            method="POST",
            endpoint="/orders",
            params={},
            body=payload,
            surface="admin",
            description=description or "Create order",
            requires_resolution=requires_resolution or [],
        )

    def historical_product_search(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.5 — Thin wrapper over products_advanced for historical searches."""
        return self.products_advanced(
            body=body,
            description=description or "Historical product search",
            requires_resolution=requires_resolution or [],
        )

    # ── Customers ───────────────────────────────────────────────────────────

    def fetch_customer(
        self,
        customer_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Rows 6.1 & 6.2 — GET /customers/{customer_id}"""
        return WooAPICall(
            method="GET",
            endpoint=f"/customers/{customer_id}",
            params={},
            surface="admin",
            description=description or f"Fetch customer id={customer_id}",
            requires_resolution=requires_resolution or [],
        )

    def list_customers_search(
        self,
        search: str,
        role: str = "all",
        per_page: int = 5,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Search customers by name/email/company — GET /customers?search=..."""
        return WooAPICall(
            method="GET",
            endpoint="/customers",
            params={"search": search, "role": role, "per_page": per_page},
            surface="admin",
            description=description or f"Search customers: '{search}'",
            requires_resolution=requires_resolution or [],
        )

    def order_stats_by_rep(
        self,
        requesting_customer_id,
        date_after: Optional[str] = None,
        date_before: Optional[str] = None,
        rep: Optional[Union[str, List[str]]] = None,
        statuses: Optional[List[str]] = None,
        include_orders: bool = False,
        page: int = 1,
        per_page: Optional[int] = None,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """GET /order-stats-by-rep — sample/order counts grouped by credited rep.

        Aggregates on `_billing_project_rep` (credited orders) UNION orders
        the rep placed for herself, the same merge `get_user_orders` uses —
        so counts and, when `include_orders` is set, the order rows returned
        alongside them never disagree about which orders belong to a rep.

        `rep` accepts an email OR a display name; the plugin resolves it and
        returns 404/409 rather than guessing between two similarly-named reps.

        `include_orders` only does anything when `rep` is also set — the
        no-rep breakdown is a SQL GROUP BY with no order objects behind it.
        When set, the response carries an `orders` array (raw WC REST order
        shape) for the requested `page` ONLY, plus `page`/`per_page`/
        `total_pages`. Paging does not affect the totals: those always cover
        the whole window, scanned up to the same 2,000-order cap.

        Admin-gated for cross-rep figures; a rep gets their own only.
        Response carries `truncated` and `counted_statuses` — surface both
        rather than presenting the totals bare.
        """
        params = {"customer_id": requesting_customer_id}
        if date_after:
            params["after"] = date_after
        if date_before:
            params["before"] = date_before
        if rep:
            # A list rides as one comma-separated `rep` param — the plugin
            # resolves each name and runs ONE merged query, so the combined
            # list can be paged and deduped. Sending a request per rep would
            # give N result sets that cannot be paged together.
            if isinstance(rep, (list, tuple, set)):
                _reps = [str(r).strip() for r in rep if str(r).strip()]
            else:
                _reps = [str(rep).strip()] if str(rep).strip() else []
            if _reps:
                params["rep"] = ",".join(_reps)
        if statuses:
            params["status"] = ",".join(statuses)
        if include_orders:
            params["list"] = "1"
            # Paging applies to the ROWS only. The totals in the response are
            # always computed over the whole window, so page 3 of a 4-page
            # list still reports the same total_orders as page 1.
            params["page"] = page
            if per_page:
                params["per_page"] = per_page

        return WooAPICall(
            method="GET",
            endpoint="/order-stats-by-rep",
            params=params,
            surface="custom_plugin",
            description=description or "Order/sample counts by rep",
            requires_resolution=requires_resolution or [],
        )

    def list_rep_orders(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List orders assigned to/placed for a rep's customer — POST /orders (custom_plugin)"""
        return WooAPICall(
            method="POST",
            endpoint="/orders",
            params={},
            body=body,
            surface="custom_plugin",
            description=description or "Rep-assigned order list",
            requires_resolution=requires_resolution or [],
        )

    def update_customer(
        self,
        customer_id: int,
        payload: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 6.3 — Update a customer's profile."""
        return WooAPICall(
            method="PUT",
            endpoint=f"/customers/{customer_id}",
            params={},
            body=payload,
            surface="admin",
            description=description or f"Update customer id={customer_id}",
            requires_resolution=requires_resolution or [],
        )

    # ── Additional (not in CSV mapping; present in codebase) ────────────────

    def list_coupons(
        self,
        page: int,
        per_page: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List available coupon codes — GET /coupons"""
        return WooAPICall(
            method="GET",
            endpoint="/coupons",
            params={"per_page": per_page, "page": page},
            surface="admin",
            description=description or "List available coupon codes",
            requires_resolution=requires_resolution or [],
        )

    def fetch_wishlist(
        self,
        customer_id,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Fetch customer wishlist — POST /wishlist"""
        return WooAPICall(
            method="POST",
            endpoint="/wishlist",
            params={"customer_id": customer_id},
            surface="admin",
            description=description or "Get customer wishlist",
            requires_resolution=requires_resolution or [],
        )

    def list_products_on_sale(
        self,
        page: int,
        per_page: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List products on sale — GET /products?on_sale=true"""
        return WooAPICall(
            method="GET",
            endpoint="/products",
            params={"on_sale": "true", "per_page": per_page, "page": page, "status": "publish"},
            surface="admin",
            description=description or "List products on sale",
            requires_resolution=requires_resolution or [],
        )

    def list_attribute_terms(
        self,
        attribute_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List terms for a product attribute — GET /products/attributes/{id}/terms"""
        return WooAPICall(
            method="GET",
            endpoint=f"/products/attributes/{attribute_id}/terms",
            params={"per_page": 100},
            surface="admin",
            description=description or f"List terms for attribute id={attribute_id}",
            requires_resolution=requires_resolution or [],
        )

    def search_products(
        self,
        search_term: str,
        page: int,
        per_page: int,
        status: str = "publish",
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Text search for products — GET /products?search=..."""
        return WooAPICall(
            method="GET",
            endpoint="/products",
            params={"search": search_term, "per_page": per_page, "page": page, "status": status},
            surface="admin",
            description=description or f"Text search for '{search_term}'",
            requires_resolution=requires_resolution or [],
        )

    def list_cs_orders(
        self,
        body: dict,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """List orders via custom plugin (CS-rep role) — POST /orders (custom_plugin surface)"""
        return WooAPICall(
            method="POST",
            endpoint="/orders",
            params={},
            body=body,
            surface="custom_plugin",
            description=description or "CS rep order list",
            requires_resolution=requires_resolution or [],
        )
    
    def fetch_saved_addresses(
        self,
        requesting_customer_id,
        target_customer_id=None,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """GET /saved-addresses (custom_plugin surface)

        THWMA lets one customer keep several shipping addresses
        (`thwma_custom_address` → shipping → address_0, address_1, ...), which
        vanilla WooCommerce cannot represent — its customer record has exactly
        one shipping block.

        target_customer_id reads ANOTHER customer's address book — what the
        bulk flow needs, since the recipient comes from a company roster rather
        than from whoever holds the session. Omit it to read the requester's
        own list.

        requesting_customer_id is REQUIRED and carries authorization: these
        calls have no WP session, so the plugin cannot use get_current_user_id()
        and gates on this param instead — same convention as
        search_customers_by_company.
        """
        params = {"customer_id": requesting_customer_id}
        if target_customer_id:
            params["target_customer_id"] = target_customer_id
        return WooAPICall(
            method="GET",
            endpoint="/saved-addresses",
            params=params,
            surface="custom_plugin",
            description=description or f"Fetch saved addresses for customer {target_customer_id or 'self'}",
            requires_resolution=requires_resolution or [],
        )

    def fetch_company_order_addresses(
        self,
        company_name: str,
        limit: int = 100,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """GET /company-order-addresses (custom_plugin surface)

        Shipping destinations a company has actually had goods sent to, taken
        from order history — the same source the storefront's company address
        picker uses.

        This is the ONLY source that shows several addresses for one person:
        `/customers/by-company` returns a customer's single account address,
        and the THWMA address book is empty on this store. Rows carry
        customer_id (0 on guest orders), so a chosen address can still be tied
        to an account.

        Addresses come from past checkouts, so they include whatever was typed
        at the time. Offer them as suggestions, not as validated data.
        """
        return WooAPICall(
            method="GET",
            endpoint="/company-order-addresses",
            params={"company": company_name, "limit": limit},
            surface="custom_plugin",
            description=description or f"Order-history addresses for '{company_name}'",
            requires_resolution=requires_resolution or [],
        )

    def search_customers_by_company(
        self,
        company_name: str,
        per_page: int = 20,
        page: int = 1,
        requesting_customer_id=None,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """GET /customers/by-company?name=<company>&page=<n> (custom_plugin surface)

        per_page defaults to 20 — the plugin's own ceiling
        (`min(20, ...)` in get_customers_by_company). The old default of 3
        silently truncated a company's contact list, so a named recipient
        could be reported "not found" purely because they sat 4th.

        That ceiling is per PAGE, not per company: 20 was still a hard stop
        because the endpoint had no offset. Callers that need the full
        membership must page through with `page` until a short page comes
        back — see the roster loop in parsers/bulk_order_parser.py, which
        also caps how far it will walk.

        requesting_customer_id is REQUIRED by the plugin: it role-gates on
        the caller and returns 403 without it.
        """
        params = {"name": company_name, "per_page": per_page, "page": page}
        if requesting_customer_id:
            params["customer_id"] = requesting_customer_id
        return WooAPICall(
            method="GET",
            endpoint="/customers/by-company",
            params=params,
            surface="custom_plugin",
            description=description or f"Search customers by company '{company_name}'",
            requires_resolution=requires_resolution or [],
        )
        
    def search_customers_by_email(
        self,
        email: str,
        per_page: int = 1,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Exact customer lookup by email — GET /customers?email=<email> (admin surface).

        WooCommerce treats email as a unique key, so this always returns 0 or 1 results.
        Prefer this over list_customers_search for email-based resolution — the ?email=
        param is an exact match, while ?search= is a fuzzy text scan across name/email/company.
        """
        return WooAPICall(
            method="GET",
            endpoint="/customers",
            params={"email": email, "per_page": per_page},
            surface="admin",
            description=description or f"Lookup customer by email '{email}'",
            requires_resolution=requires_resolution or [],
        )

    def search_orders_by_product(
        self,
        product_id: int,
        per_page: int = 3,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """GET /orders?product=<id>&orderby=date&order=desc (admin surface).

        Returns recent orders that contain this product, regardless of which
        customer placed them — used to surface rep-facing order history on
        product search results.
        """
        return WooAPICall(
            method="GET",
            endpoint="/orders",
            params={
                "product":  product_id,
                "per_page": per_page,
                "orderby":  "date",
                "order":    "desc",
            },
            surface="admin",
            description=description or f"Recent orders for product_id={product_id}",
            requires_resolution=requires_resolution or [],
        )
    # ── Response parsers ────────────────────────────────────────────────────
    # Each parser takes the raw ``woo_client.execute(...).get("data")`` dict
    # (or list) and returns a backend-neutral dict (or list of dicts).
    # Every result includes ``_raw`` with the original response so callers can
    # access any not-yet-normalized WooCommerce-specific field during migration.

    def parse_product(self, response: dict) -> dict:
        """Normalize a WooCommerce product response into a backend-neutral dict.

        Args:
            response: Raw product dict from ``woo_client.execute(...).get("data") or {}``.

        Returns:
            {
                "id": int | None,
                "price": str,       # sale_price if set, else price, else regular_price
                "in_stock": bool,   # True when stock_status == "instock"
                "_raw": dict,       # original response for migration safety
            }
        """
        price = (
            response.get("sale_price")
            or response.get("price")
            or response.get("regular_price")
            or ""
        )
        return {
            "id": response.get("id"),
            "price": price,
            "in_stock": response.get("stock_status") == "instock",
            "_raw": response,
        }

    def parse_variant(self, response: dict) -> dict:
        """Normalize a WooCommerce variation response into a backend-neutral dict.

        Args:
            response: Raw variation dict from ``woo_client.execute(...).get("data") or {}``,
                      or an individual item from a variations list.

        Returns:
            {
                "id": int | None,
                "price": str,           # sale_price if set, else price, else regular_price
                "options": dict,        # {attribute_name: option_value}
                "in_stock": bool,       # True when stock_status == "instock"
                "_raw": dict,           # original response for migration safety
            }
        """
        price = (
            response.get("sale_price")
            or response.get("price")
            or response.get("regular_price")
            or ""
        )

        # Build options dict — WooCommerce variations carry a list of attribute dicts
        # [{"name": "Color", "option": "Red"}, ...] or a flat {name: value} dict
        # (custom-plugin format).
        options: Dict[str, str] = {}
        attrs = response.get("attributes", [])
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict) and attr.get("name") and attr.get("option"):
                    options[attr["name"]] = attr["option"]
        elif isinstance(attrs, dict):
            options = {k: v for k, v in attrs.items() if v}

        return {
            "id": response.get("id"),
            "price": price,
            "options": options,
            "in_stock": response.get("stock_status") == "instock",
            "_raw": response,
        }

    def parse_list_variants(self, response: list) -> List[dict]:
        """Normalize a WooCommerce variations list into backend-neutral dicts.

        Args:
            response: Raw list from ``woo_client.execute(...).get("data") or []``.

        Returns:
            List of dicts, each in the same shape as ``parse_variant``.
        """
        if not isinstance(response, list):
            return []
        return [self.parse_variant(item) for item in response if isinstance(item, dict)]

    def parse_order(self, response: dict) -> dict:
        """Normalize a WooCommerce order response into a backend-neutral dict.

        WooCommerce uses a single ``status`` string.  A future Shopify parser
        will derive an equivalent string from ``financial_status`` +
        ``fulfillment_status`` so callers receive the same shape.

        Address sub-dicts (``billing`` / ``shipping``) are normalized to the
        six-key neutral shape via ``_normalize_woo_address``.  Callers that
        need WooCommerce-specific address fields (e.g. ``billing.first_name``,
        ``billing.email``) should read them from ``_raw["billing"]``.

        Args:
            response: Raw order dict from ``woo_client.execute(...).get("data") or {}``.

        Returns:
            {
                "id": int | None,
                "status": str,
                "billing_address": dict,   # neutral 6-key address shape
                "shipping_address": dict,  # neutral 6-key address shape
                "_raw": dict,              # original response for migration safety
            }
        """
        return {
            "id": response.get("id"),
            "status": response.get("status", ""),
            "billing_address": _normalize_woo_address(response.get("billing", {})),
            "shipping_address": _normalize_woo_address(response.get("shipping", {})),
            "_raw": response,
        }

    def parse_customer(self, response: dict) -> dict:
        """Normalize a WooCommerce customer response into a backend-neutral dict.

        WooCommerce stores address information in two separate ``billing`` and
        ``shipping`` blocks.  The neutral shape uses ``default_address`` (the
        billing address) and ``addresses`` (a list containing both addresses,
        deduplicated when they are identical).

        A future Shopify parser will map ``default_address`` and ``addresses[]``
        from Shopify's native format into the same shape.

        Callers that need WooCommerce-specific address fields (e.g.
        ``billing.first_name``, ``billing.phone``) should read them from
        ``_raw["billing"]``.

        Args:
            response: Raw customer dict from ``woo_client.execute(...).get("data") or {}``.

        Returns:
            {
                "id": int | None,
                "first_name": str,
                "last_name": str,
                "email": str,
                "default_address": dict,   # neutral 6-key shape (billing)
                "addresses": list[dict],   # [billing] or [billing, shipping] if different
                "_raw": dict,              # original response for migration safety
            }
        """
        billing = _normalize_woo_address(response.get("billing", {}))
        shipping = _normalize_woo_address(response.get("shipping", {}))

        # Include shipping only when it differs from billing and is non-empty
        addresses = [billing]
        if shipping != billing and any(shipping.values()):
            addresses.append(shipping)

        return {
            "id": response.get("id"),
            "first_name": response.get("first_name", ""),
            "last_name": response.get("last_name", ""),
            "email": response.get("email", ""),
            "default_address": billing,
            "addresses": addresses,
            "_raw": response,
        }

    def parse_list_published_products(self, response: list) -> List[dict]:
        """Normalize a WooCommerce published-products list into backend-neutral dicts.

        WooCommerce expresses stock availability as ``stock_status == "instock"``.
        A future Shopify parser will derive the same ``in_stock: bool`` from
        ``inventory_quantity > 0`` so callers receive an identical shape.

        Args:
            response: Raw list from ``woo_client.execute(...).get("data") or []``.

        Returns:
            List of dicts:
            {
                "id": int | None,
                "price": str,       # sale_price if set, else price, else regular_price
                "in_stock": bool,   # True when stock_status == "instock"
                "_raw": dict,       # original item for migration safety
            }
        """
        if not isinstance(response, list):
            return []
        return [
            {
                "id": item.get("id"),
                "price": (
                    item.get("sale_price")
                    or item.get("price")
                    or item.get("regular_price")
                    or ""
                ),
                "in_stock": item.get("stock_status") == "instock",
                "_raw": item,
            }
            for item in response
            if isinstance(item, dict)
        ]