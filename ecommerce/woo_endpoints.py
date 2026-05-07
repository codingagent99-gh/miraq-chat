from __future__ import annotations

from models import WooAPICall

from ecommerce.woo_adapters import normalize_call_payload


class WooEndpoints:
    def _call(self, *, method: str, endpoint: str, operation: str, params: dict | None = None,
              body: dict | None = None, description: str = "", surface: str = "admin",
              requires_resolution: list[str] | None = None) -> WooAPICall:
        return WooAPICall(
            method=method,
            endpoint=endpoint,
            params=params or {},
            body=normalize_call_payload(operation, body),
            description=description,
            requires_resolution=requires_resolution or [],
            surface=surface,
            operation=operation,
        )

    def fetch_product(self, product_id: int, *, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint=f"products/{product_id}", operation="fetch_product", description=description or f"Fetch product {product_id}")

    def list_products(self, *, page: int = 1, per_page: int = 20, search=None, on_sale=None, description: str = "", requires_resolution=None) -> WooAPICall:
        params = {"per_page": per_page, "page": page, "status": "publish"}
        if search:
            params["search"] = search
        if on_sale is not None:
            params["on_sale"] = "true" if on_sale else "false"
        return self._call(method="GET", endpoint="products", operation="list_products", params=params, description=description or "List products", requires_resolution=requires_resolution)

    def fetch_variant(self, product_id: int, variant_id: int, *, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint=f"products/{product_id}/variations/{variant_id}", operation="fetch_variant", description=description or f"Fetch variant {variant_id}")

    def list_variants(self, product_id: int, *, page: int = 1, per_page: int = 100, status: str = "publish", description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint=f"products/{product_id}/variations", operation="list_variants", params={"per_page": per_page, "page": page, "status": status}, description=description or f"List variants for product {product_id}")

    def products_advanced(self, body: dict, *, description: str = "", requires_resolution=None) -> WooAPICall:
        return self._call(method="POST", endpoint="products-advanced-new", operation="products_advanced", body=body, description=description or "Advanced product filter", surface="custom_plugin", requires_resolution=requires_resolution)

    def fetch_order(self, order_id: int, *, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint=f"orders/{order_id}", operation="fetch_order", description=description or f"Fetch order {order_id}")

    def list_customer_orders(self, *, customer_id: object, page: int = 1, per_page: int = 20, after=None, before=None, include=None, description: str = "", requires_resolution=None) -> WooAPICall:
        params = {"customer": customer_id, "per_page": per_page, "page": page, "orderby": "date", "order": "desc"}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        if include:
            params["include"] = include
        return self._call(method="GET", endpoint="orders", operation="list_customer_orders", params=params, description=description or "List customer orders", requires_resolution=requires_resolution)

    def list_customer_orders_custom(self, body: dict, *, description: str = "", requires_resolution=None) -> WooAPICall:
        return self._call(method="POST", endpoint="orders", operation="list_customer_orders_custom", body=body, surface="custom_plugin", description=description or "List customer orders", requires_resolution=requires_resolution)

    def create_order(self, payload: dict, *, description: str = "", requires_resolution=None) -> WooAPICall:
        return self._call(method="POST", endpoint="orders", operation="create_order", body=payload, description=description or "Create order", requires_resolution=requires_resolution)

    def fetch_customer(self, customer_id: int, *, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint=f"customers/{customer_id}", operation="fetch_customer", description=description or f"Fetch customer {customer_id}")

    def update_customer(self, customer_id: int, payload: dict, *, description: str = "") -> WooAPICall:
        return self._call(method="PUT", endpoint=f"customers/{customer_id}", operation="update_customer", body=payload, description=description or f"Update customer {customer_id}")

    def list_categories(self, *, page: int = 1, per_page: int = 100, hide_empty: bool = True, orderby: str = "name", order: str = "asc", description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint="products/categories", operation="list_categories", params={"per_page": per_page, "page": page, "hide_empty": hide_empty, "orderby": orderby, "order": order}, description=description or "List categories")

    def list_tags(self, *, page: int = 1, per_page: int = 100, hide_empty: bool = True, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint="products/tags", operation="list_tags", params={"per_page": per_page, "page": page, "hide_empty": hide_empty}, description=description or "List tags")

    def list_attribute_terms(self, attribute_id: int, *, per_page: int = 100, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint=f"products/attributes/{attribute_id}/terms", operation="list_attribute_terms", params={"per_page": per_page}, description=description or "List attribute terms")

    def list_coupons(self, *, page: int = 1, per_page: int = 20, description: str = "") -> WooAPICall:
        return self._call(method="GET", endpoint="coupons", operation="list_coupons", params={"per_page": per_page, "page": page}, description=description or "List coupons")

    def fetch_wishlist(self, customer_id: object, *, description: str = "") -> WooAPICall:
        return self._call(method="POST", endpoint="wishlist", operation="fetch_wishlist", params={"customer_id": customer_id}, description=description or "Fetch wishlist")
