"""
tenant_config.py — Per-tenant credentials and URL bundle.

Built per-tenant from the tenants table by tenant_registry.py's
_rehydrate(). (Phase 1/2 briefly had a from_env() bridge for the
single-tenant case before any Tenant row existed; Phase 3's
initialize_store() rewrite replaced the eager single-tenant boot with the
tenant/engine registries and per-request resolution, so nothing needs that
bridge anymore — removed rather than left as unreachable code.)
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
    license_id: str = ""
    tenant_id: str = ""

    # Backend selector
    ecommerce_backend: str = "woocommerce"  # "woocommerce" | "shopify"

    # Shopify — per-tenant values only.
    #
    # shopify_client_id / shopify_client_secret are deliberately absent: they
    # identify the MiraQ app, not the store, and are identical for every
    # tenant (see app_config.SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET).
    # That same secret is what verifies App Proxy signatures and webhook
    # HMACs for ALL stores, so a per-tenant copy would be N duplicates of one
    # value — rotating it would mean rewriting every row. Read them from
    # app_config at the point of use.
    shopify_domain: str = ""

    # Local-dev escape hatch only: a manually pasted Admin API token, used by
    # StoreLoader when no ShopifyTokenManager is running. In production the
    # token comes from models/shopify_token.py keyed by shopify_domain.
    shopify_admin_token: str = ""