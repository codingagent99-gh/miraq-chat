from typing import cast
from ecommerce.endpoints import EcommerceEndpoints
from platform_config import ECOMMERCE_BACKEND, VALID_ECOMMERCE_BACKENDS

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
        # A Shopify GID in the arguments is positive evidence, so it still
        # wins. Note this only ever fires for STRING args: a plain int id
        # (e.g. endpoints.fetch_customer(customer_id=int(rep_id))) cannot
        # self-route this way and depends entirely on the deployment constant
        # below -- which is how a Shopify customer id ended up being fetched
        # from WooCommerce.
        for arg in args:
            if isinstance(arg, str) and arg.startswith("gid://"):
                return "shopify"
        for val in kwargs.values():
            if isinstance(val, str) and val.startswith("gid://"):
                return "shopify"

        # Read the override INSIDE the try (touching `g` with no application
        # context raises RuntimeError from werkzeug's proxy, and getattr's
        # default does not suppress it), but VALIDATE it outside -- otherwise
        # the except clause swallows our own rejection along with it.
        override = None
        try:
            from flask import g, has_app_context
            if has_app_context():
                override = getattr(g, "ecommerce_backend", None)
        except Exception:
            override = None

        if override:
            override = str(override).strip().lower()
            if override not in VALID_ECOMMERCE_BACKENDS:
                raise RuntimeError(
                    f"g.ecommerce_backend={override!r} is not a recognised "
                    "backend. Valid values: "
                    + ", ".join(sorted(VALID_ECOMMERCE_BACKENDS))
                )
            return override

        return ECOMMERCE_BACKEND

    def __getattr__(self, name):
        """Intercepts all endpoint methods and resolves them at call-time."""
        def wrapper(*args, **kwargs):
            backend_name = self._determine_backend(*args, **kwargs)
            target_backend = self._backends.get(backend_name)
            # Previously this defaulted to the WooCommerce backend on any
            # unknown name, so a misconfigured deployment issued real HTTP
            # calls against an unrelated store instead of failing. Raise
            # instead: platform_config already guarantees the deployment
            # constant is valid, so reaching here means a genuine bug.
            if target_backend is None:
                raise RuntimeError(
                    f"No e-commerce backend registered for {backend_name!r} "
                    f"(resolving {name!r}). Registered: "
                    + ", ".join(sorted(self._backends))
                )
            method = getattr(target_backend, name)
            return method(*args, **kwargs)
        return wrapper

# Use typing.cast to satisfy Pylance/Mypy static analysis.
# This forces the type checker to treat the proxy as a full EcommerceEndpoints instance,
# eliminating the error while maintaining autocomplete everywhere else!
endpoints: EcommerceEndpoints = cast(EcommerceEndpoints, DynamicEndpointsRouter())