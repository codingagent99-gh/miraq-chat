"""
ecommerce/__init__.py — Selects and exports the active e-commerce endpoint factory.

The ``endpoints`` singleton is selected at import time via the ``ECOMMERCE_BACKEND``
environment variable (default: ``"woocommerce"``).

Usage::

    from ecommerce import endpoints

    call = endpoints.fetch_product(product_id=123)
    result = woo_client.execute(call)

    # OR for Shopify product queries:
    from api_builder.shopify_executor import ShopifyQueryExecutor
    executor = ShopifyQueryExecutor(store_loader)
    result   = executor.execute(conditions, page=1, per_page=20)

Adding a new backend
--------------------
1. Create ``ecommerce/<name>_endpoints.py`` satisfying ``EcommerceEndpoints``.
2. Add ``elif _BACKEND == "<name>": ...`` in ``get_endpoints()`` below.
3. If the backend needs an in-memory executor, create
   ``api_builder/<name>_executor.py`` implementing ``QueryExecutor``.

No other files need to change.
"""

import os

from ecommerce.endpoints import EcommerceEndpoints  # noqa: F401 — re-exported for type hints


def get_endpoints() -> EcommerceEndpoints:
    """Return the endpoint factory for the configured e-commerce backend.

    Raises ``ValueError`` for unknown ``ECOMMERCE_BACKEND`` values so
    misconfiguration is detected at startup rather than at the first API call.
    """
    backend = os.getenv("ECOMMERCE_BACKEND", "woocommerce").lower()

    if backend == "woocommerce":
        from ecommerce.woo_endpoints import WooEndpoints
        return WooEndpoints()

    if backend == "shopify":
        from ecommerce.shopify_endpoints import ShopifyEndpoints
        return ShopifyEndpoints()

    raise ValueError(
        f"Unknown ECOMMERCE_BACKEND={backend!r}. "
        "Supported values: 'woocommerce', 'shopify'."
    )


# Module-level singleton — created once at import time.
endpoints: EcommerceEndpoints = get_endpoints()