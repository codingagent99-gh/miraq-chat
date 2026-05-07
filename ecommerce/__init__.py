import os

from ecommerce.endpoints import EcommerceEndpoints
from ecommerce.woo_endpoints import WooEndpoints


backend = os.getenv("ECOMMERCE_BACKEND", "woocommerce").lower()
if backend != "woocommerce":
    raise ValueError(f"Unsupported ecommerce backend: {backend}")

endpoints: EcommerceEndpoints = WooEndpoints()

__all__ = ["endpoints", "EcommerceEndpoints", "WooEndpoints"]
