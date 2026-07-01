"""
tenant_config.py — Per-tenant credentials and URL bundle.

Phase 1: one instance, seeded from .env via TenantConfig.from_env().
Phase 2+: one instance per live tenant, loaded from the tenant DB.
"""

from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass
class TenantConfig:
    # WordPress root URL — used for plugin/widget endpoints that sit above /wp-json
    wp_base_url: str           # e.g. https://wgc.net.in/hn

    # WooCommerce REST surfaces
    woo_base_url: str          # .../wp-json/wc/v3
    woo_store_api_url: str     # .../wp-json/wc/store/v1
    custom_api_base_url: str   # .../wp-json/custom-api/v1

    # WooCommerce credentials
    woo_key: str
    woo_secret: str

    # Backend selector
    ecommerce_backend: str = "woocommerce"  # "woocommerce" | "shopify"

    # Shopify (deferred to Phase 2+ for multi-tenant; kept here for single-tenant parity)
    shopify_domain: str = ""
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_admin_token: str = ""

    @classmethod
    def from_env(cls) -> TenantConfig:
        """Construct from environment variables — used to seed the Phase-1 single-tenant registry."""
        wp_base = os.getenv("WP_BASE_URL", "https://wgc.net.in/hn")
        return cls(
            wp_base_url=wp_base,
            woo_base_url=os.getenv("WOO_BASE_URL",         f"{wp_base}/wp-json/wc/v3"),
            woo_store_api_url=os.getenv("WOO_STORE_API_URL",  f"{wp_base}/wp-json/wc/store/v1"),
            custom_api_base_url=os.getenv("CUSTOM_API_BASE_URL", f"{wp_base}/wp-json/custom-api/v1"),
            woo_key=os.getenv("WOO_CONSUMER_KEY",    ""),
            woo_secret=os.getenv("WOO_CONSUMER_SECRET", ""),
            ecommerce_backend=os.getenv("ECOMMERCE_BACKEND", "woocommerce").lower(),
            shopify_domain=os.getenv("SHOPIFY_STORE_DOMAIN", ""),
            shopify_client_id=os.getenv("SHOPIFY_CLIENT_ID",     ""),
            shopify_client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", ""),
            shopify_admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN",  ""),
        )