"""
ecommerce/cart_actions.py — backend-correct add-to-cart action construction.

Why this exists
───────────────
The two cart contracts are NOT interchangeable:

  WooCommerce → ADD_TO_CART { product_id, variation_id, variation[] }
                The frontend passes product_id first and variation_id third;
                the Woo adapter needs both.

  Shopify     → SHOPIFY_ADD_TO_CART { variant_id, variant_numeric_id }
                The Shopify adapter's addItem() treats its FIRST argument as
                the VARIANT identifier and ignores variationId entirely
                (platform/shopify/useCart.ts). Shopify cart lines are keyed by
                variant — a product id is not addressable.

Previously ``handlers/cart_handler.py`` always emitted the Woo shape, and
``routes/chat.py`` only emitted the Shopify shape when the *variation id*
happened to be a GID string. So any Shopify add-to-cart that arrived with a
product GID and no resolved variant (simple products, and the direct
"add X to cart" intent) sent a PRODUCT gid into the variant slot, where
toVariantId() reduced it to a product numeric and /cart/add.js rejected it.

This module is the single place that decides the shape, so neither caller has
to know the difference — and adding a third backend later means editing one
function rather than hunting call sites.
"""

from typing import Any, Dict, Optional, Tuple

from app_config import ECOMMERCE_BACKEND
from chat_logger import get_logger
from core.actions import build_add_to_cart, build_shopify_add_to_cart

logger = get_logger("miraq_chat")

# Reasons an action could not be built (returned so callers can respond
# appropriately rather than guessing).
UNRESOLVED_VARIANT = "unresolved_variant"


def _as_variant_gid(raw) -> Optional[str]:
    """Normalise a variant reference to a ProductVariant GID.

    Accepts an existing GID or a bare numeric id (the frontend reduces GIDs to
    numerics anyway, so either form works downstream — but the action payload
    is specified in GID terms, so we normalise here).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("gid://"):
        return s
    if s.isdigit():
        return f"gid://shopify/ProductVariant/{s}"
    return None


def resolve_shopify_variant_gid(
    *,
    product_id,
    variant_id=None,
    resolved_attrs: Optional[Dict[str, str]] = None,
    store_loader=None,
) -> Optional[str]:
    """Resolve the ProductVariant GID to add to the cart, or None.

    Priority:
      1. An explicit variant reference — the variant flow already resolved it.
      2. Attribute match against the product's variations (case-insensitive).
      3. Single-variation product — unambiguous, so use it. This is the
         "simple product" case that previously failed: Shopify models every
         product as having at least one variant, and the loader carries it.

    Returns None when the product has several variations and nothing selects
    between them; the caller must then ask the shopper to choose rather than
    guessing (adding the wrong size/colour silently is worse than a prompt).
    """
    explicit = _as_variant_gid(variant_id)
    if explicit:
        return explicit

    if not store_loader:
        return None

    p_id_str = str(product_id) if product_id is not None else ""
    product = None
    for candidate in (getattr(store_loader, "products", None) or []):
        if (str(candidate.get("id", "")) == p_id_str
                or str(candidate.get("_shopify_gid", "")) == p_id_str):
            product = candidate
            break

    if not product:
        logger.warning(
            f"resolve_shopify_variant_gid: product {product_id!r} not in store loader"
        )
        return None

    variations = [v for v in (product.get("variations") or []) if isinstance(v, dict)]
    if not variations:
        return None

    if resolved_attrs:
        for var in variations:
            var_opts = {
                a.get("name", "").lower(): a.get("option", "").lower()
                for a in (var.get("attributes") or [])
                if isinstance(a, dict)
            }
            if all(
                var_opts.get(str(k).lower()) == str(v).lower()
                for k, v in resolved_attrs.items()
            ):
                return _as_variant_gid(var.get("_shopify_gid") or var.get("id"))

    if len(variations) == 1:
        return _as_variant_gid(variations[0].get("_shopify_gid") or variations[0].get("id"))

    logger.info(
        f"resolve_shopify_variant_gid: {len(variations)} variations for "
        f"product {product_id!r} and no selection — cannot resolve"
    )
    return None


def build_cart_add_action(
    *,
    product_id,
    quantity: int,
    name: Optional[str] = None,
    variation_id=None,
    resolved_attrs: Optional[Dict[str, str]] = None,
    store_loader=None,
    build_variation_payload: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build the add-to-cart action for the active backend.

    Returns ``(action, error)``. On success ``error`` is None; on failure
    ``action`` is None and ``error`` explains why (currently only
    ``UNRESOLVED_VARIANT``), so the caller can prompt instead of emitting an
    action that is guaranteed to fail in the browser.

    ``build_variation_payload`` applies to the WooCommerce path only and
    exists to preserve each caller's exact pre-existing behaviour: the
    confirm-add-to-cart flow in chat.py has always attached the resolved
    variation attributes, while the direct ADD_TO_CART intent in
    cart_handler.py has always emitted the action without them (and calling
    the Woo payload builder there would introduce a new API call). It is
    ignored on Shopify, where the variant GID carries all option values.
    """
    if ECOMMERCE_BACKEND == "shopify":
        variant_gid = resolve_shopify_variant_gid(
            product_id=product_id,
            variant_id=variation_id,
            resolved_attrs=resolved_attrs,
            store_loader=store_loader,
        )
        if not variant_gid:
            return None, UNRESOLVED_VARIANT
        return build_shopify_add_to_cart(
            variant_gid=variant_gid,
            quantity=quantity,
            name=name,
        ), None

    # ── WooCommerce (unchanged contract) ──────────────────────────────────
    variation_attributes = []
    if build_variation_payload:
        try:
            from ecommerce import endpoints
            variation_attributes = endpoints.build_cart_variation_payload(
                product_id=product_id,
                variant_id=variation_id,
                resolved_attrs=resolved_attrs or {},
                store_loader=store_loader,
            )
        except Exception as exc:
            logger.warning(f"build_cart_add_action: variation payload failed | {exc}")
            variation_attributes = []

    return build_add_to_cart(
        product_id=product_id,
        quantity=quantity,
        name=name,
        variation_id=variation_id,
        variation=variation_attributes,
    ), None