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
    # Two passes, GID first. Product ids are now the numeric tail of the
    # Shopify GID rather than a positional counter, so a product numeric and
    # a variant numeric live in the same value space and could in principle
    # coincide. An exact GID match is unambiguous by construction, so it must
    # never lose to a numeric one — hence the ordering rather than a single
    # combined `or`.
    for candidate in (getattr(store_loader, "products", None) or []):
        if p_id_str and str(candidate.get("_shopify_gid", "")) == p_id_str:
            product = candidate
            break
    if product is None:
        for candidate in (getattr(store_loader, "products", None) or []):
            if p_id_str and str(candidate.get("id", "")) == p_id_str:
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
    suppress_result: bool = False,
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

    ``suppress_result`` is passed straight through to whichever action
    builder runs — see build_add_to_cart() for what it does and why a
    multi-line caller (bulk order) sets it while a single-item caller
    (confirm_add_to_cart) does not.
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
            suppress_result=suppress_result,
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

        # The payload builder drops attributes the product does not vary on.
        # If that leaves nothing AND no variation was resolved, the add would
        # reach the browser as a bare variable-product add and fail there —
        # the same silent-in-the-backend failure as sending a bogus attribute.
        # Prompt instead, using the contract the caller already handles.
        if resolved_attrs and not variation_attributes and not variation_id:
            logger.info(
                f"build_cart_add_action: no usable variation attributes left for "
                f"product_id={product_id} (asked for {sorted(resolved_attrs)}) — "
                f"prompting instead of emitting an add that would fail"
            )
            return None, UNRESOLVED_VARIANT

        # The opposite failure, and the one that produced
        # "woocommerce_rest_missing_variation_data / Finish and Colors are
        # required fields": TOO FEW axes.
        #
        # wc/v3 omits "Any" axes from a variation's own attribute list, so a
        # self-contained sample variation (Tara chip card, 17132) reports only
        # Sample Size while the parent varies on Sample Size, Finish AND
        # Colors. The orders API accepts that; the Store API does not — it
        # requires a non-empty value for every one of the parent's variation
        # attributes. So the same variation can be ordered by a rep and
        # refused by a shopper's cart.
        #
        # Emitting it anyway pushes a raw storefront error into the widget
        # with nothing in the backend log. Prompting is not the ideal answer
        # for a chip card — the whole point of that variation is not having to
        # pick a colour — but it beats an add that cannot succeed. The real
        # fix is store-side: give those axes explicit terms instead of "Any".
        if variation_attributes:
            try:
                from ecommerce import endpoints
                _required = endpoints.product_variation_taxonomies(product_id, store_loader)
            except Exception:
                _required = None
            if _required:
                _supplied = {
                    a.get("attribute") for a in variation_attributes
                    if a.get("attribute") and a.get("value")
                }
                _missing = set(_required) - _supplied
                if _missing:
                    logger.warning(
                        f"build_cart_add_action: variation {variation_id} for product "
                        f"{product_id} leaves {sorted(_missing)} unset, but the Store API "
                        f"requires every parent axis — prompting instead of emitting an "
                        f"add that would 400. Fix store-side by giving those axes explicit "
                        f"terms rather than 'Any'."
                    )
                    return None, UNRESOLVED_VARIANT

    return build_add_to_cart(
        product_id=product_id,
        quantity=quantity,
        name=name,
        variation_id=variation_id,
        variation=variation_attributes,
        suppress_result=suppress_result,
    ), None