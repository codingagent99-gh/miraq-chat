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
from typing import Dict, List, Optional

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
            endpoint="/catalog/currency",
            params={},
            surface="custom_plugin",
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
            endpoint="/catalog/categories",
            params=params,
            surface="custom_plugin",
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
            endpoint="/catalog/tags",
            params=params,
            surface="custom_plugin",
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
            endpoint="/catalog/products",
            params={"status": "publish", "per_page": per_page, "page": page},
            surface="custom_plugin",
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
            endpoint=f"/catalog/products/{product_id}",
            params={},
            surface="custom_plugin",
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
            endpoint=f"/catalog/products/{product_id}/variations/{variant_id}",
            params={},
            surface="custom_plugin",
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
            endpoint=f"/catalog/products/{product_id}/variations",
            params=params,
            surface="custom_plugin",
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

    def product_variation_taxonomies(self, product_id, store_loader=None):
        """Taxonomies the PARENT product varies on, or None if unknown.

        None means "could not determine" — callers must treat that as
        "do not filter, do not block", so a failed fetch degrades to the old
        behaviour rather than breaking every add.

        wc/v3 omits "Any" axes from a VARIATION's own attribute list, so the
        variation alone cannot tell you what the product requires. The Store
        API, unlike the orders API, demands a non-empty value for every one of
        these — which is why an order can be created from a variation the cart
        refuses. Cached in store_loader.product_variation_schema.
        """
        if not product_id:
            return None
        cache = getattr(store_loader, "product_variation_schema", None)
        if cache is None:
            cache = {}
        key = int(product_id)
        if key in cache:
            return cache[key].get("taxonomies")
        try:
            resp = woo_client.execute(self.fetch_product(
                product_id=product_id,
                description=f"Fetch parent attributes for cart payload {product_id}",
            ))
            data = resp.get("data") if resp.get("success") else None
            if not isinstance(data, dict):
                return None
            taxes = set()
            opts = {}
            for a in data.get("attributes") or []:
                if isinstance(a, dict) and a.get("variation"):
                    name = str(a.get("name") or "")
                    tax = (
                        name.lower() if name.lower().startswith("pa_")
                        else "pa_" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                    )
                    taxes.add(tax)
                    # Options in the order WooCommerce has them, which is the
                    # order the storefront dropdown shows — so "first" here
                    # means the same thing a shopper would see first.
                    opts[tax] = {
                        "name": name,
                        "options": [str(o) for o in (a.get("options") or [])],
                    }
            cache[key] = {"taxonomies": taxes, "axes": opts}
            try:
                store_loader.product_variation_schema = cache
            except Exception:
                pass  # loader without the attribute — still works, just uncached
            return taxes
        except Exception as exc:
            logger.warning(
                f"product_variation_taxonomies: fetch failed for {product_id} | error={exc}"
            )
            return None

    def product_variation_axes(self, product_id, store_loader=None):
        """Parent variation axes with their options, keyed by taxonomy.

        ``{"pa_colors": {"name": "Colors", "options": ["TARA Ash", ...]}}``
        Shares the cache and the fetch with product_variation_taxonomies().
        """
        self.product_variation_taxonomies(product_id, store_loader)
        cache = getattr(store_loader, "product_variation_schema", None) or {}
        try:
            return (cache.get(int(product_id)) or {}).get("axes") or {}
        except Exception:
            return {}

    def list_variants_resolved(self, product_id, store_loader=None, per_page: int = 100):
        """Fetch a product's variations over the REST API. See the Protocol.

        Deliberately uncached: both existing callers (bulk_order_parser's
        _variant_cache and bulk_order_handler's bulk_variant_cache) already
        memoise per flow, and adding a third layer with its own lifetime would
        change when a rep sees a freshly-edited variation.

        The None-vs-[] distinction is the whole point of the method. A
        transport failure previously came back as {"success": False,
        "data": []}, indistinguishable from "this product has no variations"
        once stored, which is how a dropped connection got reported to the
        user as "that option isn't in the catalog".
        """
        if not product_id:
            return None
        try:
            result = woo_client.execute(self.list_variants(
                product_id=product_id,
                per_page=per_page,
                description=f"Fetch variations for product_id={product_id}",
            ))
        except Exception as exc:
            logger.warning(
                f"list_variants_resolved: fetch raised for product_id={product_id} | {exc}"
            )
            return None

        data = result.get("data")
        if result.get("success") and isinstance(data, list):
            return data

        logger.warning(
            f"list_variants_resolved: lookup FAILED for product_id={product_id} | "
            f"error={result.get('error')}"
        )
        return None

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

        def _parent_taxonomies():
            return self.product_variation_taxonomies(product_id, store_loader)

        # A resolved attribute the product does not vary on is not a harmless
        # extra: WooCommerce's Store API rejects an add-item whose variation
        # array names an attribute the product has no matching variation for,
        # and the whole add fails.
        #
        # "order 1 tara chipcard" is the case that surfaced it — "chipcard"
        # matched BOTH a Sample Size term and a Tile Size term, both landed in
        # entities.attributes, and the payload carried pa_tile-size for a
        # product that has no such axis. The rep saw "Couldn't add Tara to
        # your cart" with nothing in the backend log to explain it, because
        # the failure happens in the browser.
        _allowed = _parent_taxonomies()

        def _keep(taxonomy: str) -> bool:
            if not _allowed:
                return True   # unknown or no variation axes — do not filter
            if taxonomy in _allowed:
                return True
            logger.info(
                f"build_cart_variation_payload: dropping {taxonomy!r} — "
                f"product {product_id} does not vary on it "
                f"(has {sorted(_allowed)})"
            )
            return False

        def _resolved_payload() -> List[Dict[str, str]]:
            out = []
            for attr_key, display_value in (resolved_attrs or {}).items():
                taxonomy = _taxonomy_for(attr_key)
                if not _keep(taxonomy):
                    continue
                out.append({"attribute": taxonomy, "value": _slug_for(attr_key, display_value)})
            return out

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
                    # Slug, not the display option. The variation reports
                    # "Chip Card"; the Store API matches on the term slug
                    # ("chip-card"), which is what the product permalink
                    # carries. _slug_for falls back to the squashed value when
                    # the term cannot be resolved, so this never sends less
                    # than before.
                    result.append({
                        "attribute": taxonomy,
                        "value": _slug_for(attr_name, option) if option else "",
                    })
                    fixed.add(taxonomy)

            for attr_key, display_value in (resolved_attrs or {}).items():
                taxonomy = _taxonomy_for(attr_key)
                if taxonomy in fixed:
                    continue
                if not _keep(taxonomy):
                    continue
                result.append({"attribute": taxonomy, "value": _slug_for(attr_key, display_value)})
                fixed.add(taxonomy)

            # ── Self-contained sample forms (Chip Card) only ─────────────────
            # This variation pins Sample Size and leaves Colors and Finish as
            # WooCommerce "Any". The orders API accepts that; the Store API
            # requires a non-empty value for every parent axis and 400s with
            # woocommerce_rest_missing_variation_data. A chip card is a card
            # of every colour, so there is no correct colour to send — but
            # sending nothing means it cannot go in a cart at all.
            #
            # So fill the unset axes with the parent's FIRST option, the same
            # value the storefront dropdown shows first. `id` is the variation
            # id, so WooCommerce pins the line to THIS variation and these
            # values ride along as item meta rather than selecting a different
            # variation.
            #
            # Deliberately narrow: gated on the variation declaring a term
            # from SELF_CONTAINED_VARIATION_TERMS, so an ordinary variation
            # that is genuinely missing a choice still prompts instead of
            # having a colour picked for the customer.
            # Local import: handlers.chat_utils pulls in store/config modules,
            # and importing it at module scope makes ecommerce → handlers →
            # ecommerce circular.
            try:
                from handlers.chat_utils import (
                    variation_declares_self_contained_term,
                    self_contained_variation_axis,
                )
                # Either the VARIATION is a dedicated sample form (Tara's
                # 17132), or the request carries a self-contained term while
                # the resolved variation is an ordinary colour one (Elizabeth,
                # London — no dedicated variation exists, so the flow falls
                # back to any variation). Both need the remaining axes filled:
                # the Store API demands every parent axis, and for a chip card
                # none of them describe what ships.
                _is_self_contained = (
                    variation_declares_self_contained_term(var_resp["data"], store_loader)
                    or bool(self_contained_variation_axis(resolved_attrs or {}, store_loader))
                )
            except Exception as exc:
                logger.warning(
                    f"build_cart_variation_payload: self-contained check failed | {exc}"
                )
                _is_self_contained = False

            if _is_self_contained:
                _axes = {}
                try:
                    _axes = self.product_variation_axes(product_id, store_loader)
                except Exception:
                    _axes = {}
                for taxonomy, meta in (_axes or {}).items():
                    if taxonomy in fixed:
                        continue
                    _options = meta.get("options") or []
                    if not _options:
                        continue
                    _first = _options[0]
                    result.append({
                        "attribute": taxonomy,
                        "value": _slug_for(meta.get("name") or taxonomy, _first),
                    })
                    fixed.add(taxonomy)
                    logger.info(
                        f"build_cart_variation_payload: self-contained variation "
                        f"{variant_id} leaves {taxonomy!r} unset — filling with the "
                        f"first option {_first!r} so the Store API accepts the add"
                    )
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
        """Row 5.1 — GET miraq/v1/my-orders

        The plugin returns only the orders of the shopper whose identity token
        woo_client forwards, newest first, in the same JSON as /wc/v3/orders.
        customer_id is kept for the description and the CURRENT_USER_ID
        resolution step; the plugin does not read it.
        """
        params = {
            "per_page": per_page,
            "page": page,
            **filters,
        }
        return WooAPICall(
            method="GET",
            endpoint="/my-orders",
            params=params,
            surface="custom_plugin",
            description=description or f"List orders for customer {customer_id}",
            requires_resolution=requires_resolution or [],
        )

    def fetch_order(
        self,
        order_id: int,
        description: str = "",
        requires_resolution: Optional[List[str]] = None,
    ) -> WooAPICall:
        """Row 5.2 — GET miraq/v1/my-orders/{order_id}

        404 unless the order belongs to the shopper whose identity token
        woo_client forwards.
        """
        return WooAPICall(
            method="GET",
            endpoint=f"/my-orders/{order_id}",
            params={},
            surface="custom_plugin",
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

    # ── Additional (not in CSV mapping; present in codebase) ────────────────



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
            endpoint="/catalog/products",
            params={"on_sale": "true", "per_page": per_page, "page": page, "status": "publish"},
            surface="custom_plugin",
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
            endpoint=f"/catalog/attributes/{attribute_id}/terms",
            params={"per_page": 100},
            surface="custom_plugin",
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
            endpoint="/catalog/products",
            params={"search": search_term, "per_page": per_page, "page": page, "status": status},
            surface="custom_plugin",
            description=description or f"Text search for '{search_term}'",
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