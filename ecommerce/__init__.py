import os
from typing import cast  # 👈 Import cast
from ecommerce.endpoints import EcommerceEndpoints

class DynamicEndpointsRouter:
    """
    A transparent routing proxy satisfying the EcommerceEndpoints protocol.
    Instead of binding to one backend at boot time, it routes calls dynamically
    at execution time based on input parameters or the active request context.
    """
    def __init__(self):
        from ecommerce.woo_endpoints import WooEndpoints
        from ecommerce.shopify_endpoints import ShopifyEndpoints
        self._backends = {
            "woocommerce": WooEndpoints(),
            "shopify": ShopifyEndpoints()
        }

    def _determine_backend(self, *args, **kwargs) -> str:
        for arg in args:
            if isinstance(arg, str) and arg.startswith("gid://"):
                return "shopify"
        for val in kwargs.values():
            if isinstance(val, str) and val.startswith("gid://"):
                return "shopify"

        try:
            from flask import g
            if hasattr(g, "ecommerce_backend"):
                return g.ecommerce_backend
        except Exception:
            pass

        return os.getenv("ECOMMERCE_BACKEND", "woocommerce").lower()

    def __getattr__(self, name):
        """Intercepts all endpoint methods and resolves them at call-time."""
        def wrapper(*args, **kwargs):
            backend_name = self._determine_backend(*args, **kwargs)
            target_backend = self._backends.get(backend_name, self._backends["woocommerce"])
            method = getattr(target_backend, name)
            return method(*args, **kwargs)
        return wrapper

# 🌟 Use typing.cast to satisfy Pylance/Mypy static analysis.
# This forces the type checker to treat the proxy as a full EcommerceEndpoints instance,
# eliminating the error while maintaining autocomplete everywhere else!
endpoints: EcommerceEndpoints = cast(EcommerceEndpoints, DynamicEndpointsRouter())