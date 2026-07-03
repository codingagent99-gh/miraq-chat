"""
tenant_config.py — Per-tenant credentials and URL bundle.

Phase 1: one instance, seeded from .env via TenantConfig.from_env().
Phase 2+: one instance per live tenant, loaded from the tenant DB.
"""

from __future__ import annotations
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
