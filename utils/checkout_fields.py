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

The union rule (the important part)
───────────────────────────────────
Required fields are the UNION of a static floor (config.store_config.
BULK_ADDRESS_REQUIRED_FLOOR) and the live /checkout-fields response. The live
response may only ADD required fields; it may never remove one.

This is not defensive over-engineering. THWCFE (the checkout field editor
plugin) evaluates conditional-display rules when woocommerce_checkout_fields
runs. In a REST context there is no session and no cart, so a condition can
evaluate false and the field is stripped from the response even though the live
checkout renders it. The widget hits exactly this and works around it in
hooks/useCheckoutFields.ts:470-501 by re-injecting billing_field_type whenever
/order-types comes back non-empty. If this module trusted the live response
verbatim, a misfiring plugin conditional would silently disable the validation
gate — the precise failure this module exists to prevent.

Key shapes
──────────
All keys here are SHORT form keys (group prefix stripped): "first_name", not
"billing_first_name". That is the shape the widget's address panel posts back
inside __BULK_ADDR__, and the shape bulk_order_handler stores in its
billing_address / shipping_address blocks. _form_key() below mirrors formKey()
in hooks/useCheckoutFields.ts — keep the two in sync.

The "meta" group holds the CS custom fields (billing_field_type,
billing_project, project_rep). They are order META, not WooCommerce address
fields, so no address-level check would ever catch them. They live on the
billing block in the panel payload, so they are validated against `billing`.
"""

import time
from typing import Optional

from chat_logger import get_logger
from config.store_config import BULK_ADDRESS_REQUIRED_FLOOR

logger = get_logger("miraq_chat")

# Cache TTL for the live /checkout-fields response.
_CACHE_TTL_SECONDS = 900  # 15 minutes

# ── Key normalisation (mirrors hooks/useCheckoutFields.ts) ────────────────────

# Custom fields whose names start with "billing_" but must keep the full name —
# stripping the prefix would make them ambiguous and break the __BULK_ADDR__
# consumer, which looks for these exact keys.
_BILLING_KEEP_PREFIX = frozenset({"billing_field_type", "billing_project"})

# THWCFE registers "<group>_company_name" instead of the standard
# "<group>_company". BOTH groups must be normalised to the same short key.
#
# Billing was missing here, so /checkout-fields' "billing_company_name" became
# the short key "company_name" on this side while the widget mapped it to
# "company". The rep would fill in Company Name, the payload would carry
# "company", and validation would look for "company_name", find it blank, and
# block the order with "Company Name cannot be edited here" — a field the card
# was in fact rendering, just under the other key.
_COMPANY_KEY_REMAP = {
    "billing_company_name": "company",
    "shipping_company_name": "company",
}

# Short keys that belong to the "meta" group rather than the address block they
# were registered under.
_META_KEYS = frozenset({"billing_field_type", "billing_project", "project_rep"})

# Display labels for the shopper-facing message. Anything unmapped falls back to
# a title-cased version of the key. Live labels from /checkout-fields override
# these when available (see _absorb_live_labels).
_DEFAULT_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "company": "Company name",
    "country": "Country",
    "address_1": "Address",
    "address_2": "Address line 2",
    "city": "City",
    "state": "State",
    "postcode": "Postcode",
    "phone": "Phone",
    "email": "Email",
    "billing_field_type": "Order Type",
    "billing_project": "Project Name",
    "project_rep": "Your Rep",
    "order_notes": "Order notes",
}

_live_labels: dict = {}

# Cache is keyed by the custom-api base URL, which is this deployment's store
# identity. On a multi-tenant build where that URL is resolved per tenant rather
# than being a module constant, this key stays correct; if it ever becomes
# ambiguous, drop the cache rather than risk serving one store's field config to
# another. One extra HTTP call per bulk order is a cheap price.
_cache: dict = {"key": None, "value": None, "expires_at": 0.0}


def _form_key(wc_key: str, group: str) -> str:
    """WooCommerce field key → short form key. Mirrors formKey() in the widget."""
    if group == "billing" and wc_key in _BILLING_KEEP_PREFIX:
        return wc_key
    remapped = _COMPANY_KEY_REMAP.get(wc_key)
    if remapped:
        return remapped
    prefix = f"{group}_"
    stripped = wc_key[len(prefix):] if wc_key.startswith(prefix) else wc_key
    # Catch any other "<group>_company_name" spelling the theme may register.
    return "company" if stripped == "company_name" else stripped


def label_for(key: str) -> str:
    """Display label for a field key."""
    return _live_labels.get(key) or _DEFAULT_LABELS.get(key) or key.replace("_", " ").title()


def _blank_floor() -> dict:
    """Deep-ish copy of the configured floor, so callers can't mutate config."""
    return {
        "billing": list(BULK_ADDRESS_REQUIRED_FLOOR.get("billing", [])),
        "shipping": list(BULK_ADDRESS_REQUIRED_FLOOR.get("shipping", [])),
        "meta": list(BULK_ADDRESS_REQUIRED_FLOOR.get("meta", [])),
    }


def _absorb_live_labels(short_key: str, cfg: dict) -> None:
    label = (cfg or {}).get("label")
    if isinstance(label, str) and label.strip():
        _live_labels[short_key] = label.strip()


def _fetch_live_fields() -> Optional[dict]:
    """
    Fetch /checkout-fields. Returns the raw grouped dict, or None on any
    failure — a plugin outage must neither break bulk ordering nor disable the
    gate, so callers fall back to the floor.
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
        logger.warning(f"[CheckoutFields] live fetch raised — using floor only | error={exc}")
        return None

    if not result.get("success") or not isinstance(result.get("data"), dict):
        logger.warning(
            "[CheckoutFields] live fetch unsuccessful — using floor only | "
            f"error={result.get('error')!r}"
        )
        return None

    return result["data"]


def get_required_fields(force_refresh: bool = False) -> dict:
    """
    Return {"billing": [...], "shipping": [...], "meta": [...]} — the union of
    the static floor and the live /checkout-fields required flags.

    Never returns fewer fields than the floor. Never raises.
    """
    from app_config import CUSTOM_API_BASE_URL

    now = time.time()
    cache_key = CUSTOM_API_BASE_URL
    if (
        not force_refresh
        and _cache["value"] is not None
        and _cache["key"] == cache_key
        and _cache["expires_at"] > now
    ):
        # Copy on the way out so a caller mutating the result can't poison the cache.
        return {k: list(v) for k, v in _cache["value"].items()}

    required = _blank_floor()
    live = _fetch_live_fields()

    if live:
        added = []
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
                # Route the CS custom fields to "meta" regardless of the address
                # group they were registered under.
                target = "meta" if short in _META_KEYS else group
                if short not in required[target]:
                    required[target].append(short)
                    added.append(f"{target}.{short}")
        # The "order" group carries order_comments / order notes, which are never
        # required for a bulk line. Deliberately ignored.
        if added:
            logger.info(
                f"[CheckoutFields] live response added {len(added)} required field(s) "
                f"beyond the floor | {', '.join(added)}"
            )
    else:
        logger.info("[CheckoutFields] falling back to static required-field floor")

    _cache["key"] = cache_key
    _cache["value"] = {k: list(v) for k, v in required.items()}
    _cache["expires_at"] = now + _CACHE_TTL_SECONDS

    return required


def validate_bulk_address(billing: dict, shipping: dict, required: dict) -> dict:
    """
    Check a single bulk line's address blocks against the required-field set.

    Returns:
        {"billing": {key: label, ...}, "shipping": {...}, "meta": {...}}

    All three groups empty means the line is valid. The "meta" group is checked
    against `billing`, because the CS custom fields live on the billing block in
    the panel payload (see _BILLING_FIELDS in handlers/bulk_order_handler.py).
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
        "meta": _missing(billing, required.get("meta")),
    }


def has_errors(errors: dict) -> bool:
    """True when validate_bulk_address() found anything missing."""
    return any((errors or {}).get(group) for group in ("billing", "shipping", "meta"))


def format_missing_fields(errors: dict) -> str:
    """
    Human-readable one-liner for the bot message, e.g.
        "Billing: City, Postcode · Shipping: Address · Project Name"
    Meta fields are listed bare — "Order Type" reads better than "Meta: Order Type".
    """
    errors = errors or {}
    parts = []
    for group, prefix in (("billing", "Billing"), ("shipping", "Shipping")):
        labels = list((errors.get(group) or {}).values())
        if labels:
            parts.append(f"{prefix}: {', '.join(labels)}")
    meta_labels = list((errors.get("meta") or {}).values())
    if meta_labels:
        parts.append(", ".join(meta_labels))
    return " · ".join(parts)


def count_missing(errors: dict) -> int:
    """Total number of missing fields across all groups."""
    errors = errors or {}
    return sum(len(errors.get(group) or {}) for group in ("billing", "shipping", "meta"))