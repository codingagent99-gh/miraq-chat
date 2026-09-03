"""
utils/checkout_fields.py — Required-field resolution and address validation for
the bulk order flow.

Why this module exists
──────────────────────
The bulk order flow places orders through POST /wc/v3/orders. That endpoint
performs NO address validation — unlike the real checkout, which runs
WC_Checkout::validate_posted_data() and enforces every `required` flag. So the
bulk flow has to do the enforcement itself, or blank-address orders get created
silently.

Note what this does and does not claim. It is not that WordPress or WooCommerce
"requires" these fields at the API layer — the REST endpoint is perfectly happy
to create an order with an empty address. It is that the STOREFRONT enforces
them and the REST path does not, so an order placed through the widget can
reach a state the same store's own checkout would have refused.

Where the required set comes from
─────────────────────────────────
Entirely from the store's own live /checkout-fields response. There is no
static list of field names anywhere in this module, and deliberately so: what
counts as required differs per store (B2B stores need company, consumer stores
do not; some countries have no postcode; a store may add fields this codebase
has never heard of). A hardcoded floor would have to be either so small it
protects nothing or so large it blocks legitimate orders on stores that do not
collect those fields.

The consequence, stated plainly: this gate is exactly as good as
/checkout-fields. If a store marks nothing required, nothing is enforced —
which is correct, because that store's own checkout would not enforce anything
either.

The unknown case
────────────────
get_required_fields() returns None — not {} — when the live set could not be
determined. The two are very different and callers MUST distinguish them:

    {}    the store was asked and requires nothing → validate, pass everything
    None  the store could not be asked → nothing is known

None deliberately carries no policy. Deciding whether an unverifiable address
should block ordering or proceed is the caller's call, and the caller is where
the user-facing consequence lives. Collapsing None into {} would silently turn
a plugin outage into "no validation" with no trace.

MULTI-TENANT NOTE
─────────────────
Every tenant has its own checkout field configuration, so the response cache is
keyed by the REQUESTING TENANT's custom-api base URL, read off the
request-scoped store loader. A process-wide cache would serve one store's
required-field set while validating another store's orders — which fails in
both directions: waving through a genuinely blank address, or blocking a
complete one. If the tenant cannot be resolved the cache is bypassed entirely
rather than shared; one extra HTTP call per bulk order is a cheap price.

Key shapes
──────────
All keys here are SHORT form keys (group prefix stripped): "first_name", not
"billing_first_name". That is the shape the widget's address panel posts back
inside __BULK_ADDR__, and the shape bulk_order_handler stores in its
billing_address / shipping_address blocks. _form_key() below mirrors formKey()
in hooks/useCheckoutFields.ts — keep the two in sync.
"""

import time
from typing import Optional

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# Cache TTL for the live /checkout-fields response.
_CACHE_TTL_SECONDS = 900  # 15 minutes

# ── Key normalisation (mirrors hooks/useCheckoutFields.ts) ────────────────────
#
# This is NOT a list of required fields — it is spelling reconciliation between
# two names for the same field. A checkout field editor plugin may register
# "<group>_company_name" instead of the standard "<group>_company"; both must
# normalise to the same short key, or the field renders under one name and
# validates under another: the shopper fills in Company Name, the payload
# carries "company", validation looks for "company_name", finds it blank, and
# blocks an order whose company field was in fact filled in.
_COMPANY_KEY_REMAP = {
    "billing_company_name": "company",
    "shipping_company_name": "company",
}

# Labels harvested from live responses, keyed by short field key. Every field
# this module validates came from the live response, so it always arrives with
# its own label — there is no bundled label table to fall out of sync with a
# store that renames its fields.
#
# Shared across tenants on purpose, and safe because it only ever affects the
# WORDING of a message, never whether a field is required. Two tenants that
# label address_1 differently will see whichever was fetched most recently —
# cosmetic, and self-correcting on the next fetch.
_live_labels: dict = {}

# Live-response cache. `key` is the tenant's custom-api base URL — see the
# multi-tenant note in the module docstring.
_cache: dict = {"key": None, "value": None, "expires_at": 0.0}


def _tenant_cache_key() -> Optional[str]:
    """Identity of the tenant this request belongs to, or None if unresolvable.

    None deliberately means "do not use the cache" rather than "use a shared
    slot": serving one store's required-field set to another is the one outcome
    worth paying an HTTP call to avoid.
    """
    try:
        from store_registry import get_store_loader

        loader = get_store_loader()
        if loader is None:
            return None
        return getattr(loader, "custom_api_base", None) or None
    except Exception:
        return None


def _form_key(wc_key: str, group: str) -> str:
    """WooCommerce field key → short form key. Mirrors formKey() in the widget."""
    remapped = _COMPANY_KEY_REMAP.get(wc_key)
    if remapped:
        return remapped
    prefix = f"{group}_"
    stripped = wc_key[len(prefix):] if wc_key.startswith(prefix) else wc_key
    # Catch any other "<group>_company_name" spelling the theme may register.
    return "company" if stripped == "company_name" else stripped


def label_for(key: str) -> str:
    """Display label for a field key — the store's own label where known."""
    return _live_labels.get(key) or key.replace("_", " ").title()


def _absorb_live_labels(short_key: str, cfg: dict) -> None:
    label = (cfg or {}).get("label")
    if isinstance(label, str) and label.strip():
        _live_labels[short_key] = label.strip()


def _fetch_live_fields() -> Optional[dict]:
    """
    Fetch /checkout-fields. Returns the raw grouped dict, or None on any
    failure. None means "could not determine", never "nothing is required".
    """
    try:
        # Imported lazily: this module is imported from handlers that are
        # themselves imported at request time, and woo_client pulls in app
        # config at import.
        from woo_client import woo_client
        from ecommerce import endpoints

        result = woo_client.execute(
            endpoints.fetch_checkout_fields(
                description="Fetch checkout fields for bulk address validation",
            )
        )
    except Exception as exc:
        logger.warning(f"[CheckoutFields] live fetch raised | error={exc}")
        return None

    if not result.get("success") or not isinstance(result.get("data"), dict):
        logger.warning(
            f"[CheckoutFields] live fetch unsuccessful | error={result.get('error')!r}"
        )
        return None

    return result["data"]


def get_required_fields(force_refresh: bool = False) -> Optional[dict]:
    """
    Return {"billing": [...], "shipping": [...]} — the fields this tenant's
    storefront marks required — or None when that could not be determined.

    An empty list for a group means the store genuinely requires nothing there.
    None means the question could not be answered at all. Never raises.
    """
    now = time.time()
    cache_key = _tenant_cache_key()

    if (
        not force_refresh
        and cache_key is not None
        and _cache["value"] is not None
        and _cache["key"] == cache_key
        and _cache["expires_at"] > now
    ):
        # Copy on the way out so a caller mutating the result can't poison the cache.
        return {k: list(v) for k, v in _cache["value"].items()}

    live = _fetch_live_fields()
    if live is None:
        # Not cached: a failure must not pin "unknown" in place for 15 minutes
        # when the plugin may recover on the very next call.
        logger.error(
            "[CheckoutFields] required-field set is UNKNOWN — /checkout-fields "
            "unavailable, so bulk addresses cannot be validated this request"
        )
        return None

    required: dict = {"billing": [], "shipping": []}
    for group in ("billing", "shipping"):
        raw_group = live.get(group)
        if not isinstance(raw_group, dict):
            continue
        for wc_key, cfg in raw_group.items():
            if not isinstance(cfg, dict):
                continue
            short = _form_key(wc_key, group)
            _absorb_live_labels(short, cfg)
            if not cfg.get("required"):
                continue
            if short not in required[group]:
                required[group].append(short)

    # The "order" group carries order_comments / order notes, which are never
    # required for a bulk line. Deliberately ignored.

    logger.info(
        f"[CheckoutFields] required set resolved | "
        f"billing={required['billing']} shipping={required['shipping']}"
    )

    if cache_key is not None:
        _cache["key"] = cache_key
        _cache["value"] = {k: list(v) for k, v in required.items()}
        _cache["expires_at"] = now + _CACHE_TTL_SECONDS

    return required


def validate_bulk_address(billing: dict, shipping: dict, required: Optional[dict]) -> dict:
    """
    Check a single bulk line's address blocks against the required-field set.

    Returns:
        {"billing": {key: label, ...}, "shipping": {...}}

    Both groups empty means nothing required is missing. A `required` of None
    (unknown) yields no errors — the caller is responsible for noticing the
    unknown and deciding what to do; see has_errors()'s docstring.
    """
    billing = billing or {}
    shipping = shipping or {}
    required = required or {}

    def _missing(block: dict, keys) -> dict:
        out = {}
        for key in keys or []:
            value = block.get(key)
            if not str(value if value is not None else "").strip():
                out[key] = label_for(key)
        return out

    return {
        "billing": _missing(billing, required.get("billing")),
        "shipping": _missing(shipping, required.get("shipping")),
    }


def has_errors(errors: dict) -> bool:
    """True when validate_bulk_address() found something missing.

    False means "nothing required is missing", which is NOT the same as "this
    address is fine" when the required set was unknown. Callers that care about
    the difference must check the required set for None themselves rather than
    reading it out of this.
    """
    return any((errors or {}).get(group) for group in ("billing", "shipping"))


def format_missing_fields(errors: dict) -> str:
    """
    Human-readable one-liner for the bot message, e.g.
        "Billing: City, Postcode · Shipping: Address"
    """
    errors = errors or {}
    parts = []
    for group, prefix in (("billing", "Billing"), ("shipping", "Shipping")):
        labels = list((errors.get(group) or {}).values())
        if labels:
            parts.append(f"{prefix}: {', '.join(labels)}")
    return " · ".join(parts)


def count_missing(errors: dict) -> int:
    """Total number of missing fields across all groups."""
    errors = errors or {}
    return sum(len(errors.get(group) or {}) for group in ("billing", "shipping"))