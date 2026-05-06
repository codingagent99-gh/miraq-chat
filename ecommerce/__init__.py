"""
ecommerce/__init__.py — Selects and exports the active e-commerce endpoint factory.

The ``endpoints`` singleton is selected at import time via the ``ECOMMERCE_BACKEND``
environment variable (default: ``"woocommerce"``).

Usage::

    from ecommerce import endpoints

    call = endpoints.fetch_product(product_id=123)
    result = woo_client.execute(call)

Adding a new backend
--------------------
1. Create ``ecommerce/shopify_endpoints.py`` with a class that satisfies
   ``EcommerceEndpoints`` (same method signatures).
2. Add a branch below: ``elif _BACKEND == "shopify": ...``

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
    raise ValueError(
        f"Unknown ECOMMERCE_BACKEND={backend!r}. "
        "Supported values: 'woocommerce'. "
        "To add Shopify support, create ecommerce/shopify_endpoints.py."
    )


# Module-level singleton — created once at import time.
endpoints: EcommerceEndpoints = get_endpoints()
