"""
parsers/bulk_order_parser.py — Parse free-text bulk order utterances into
resolved BulkOrderLine objects.

No Flask. WooCommerce API calls only (via woo_client / endpoints).

Public API:
    parse_bulk_order_utterance(text, store_loader, role, self_customer_id)
        → List[BulkOrderLine]
"""

import re
import difflib
from dataclasses import dataclass, field
from typing import Optional, List

from woo_client import woo_client
from ecommerce import endpoints
from chat_logger import get_logger
from app_config import BULK_ORDER_ROLES

logger = get_logger("miraq_chat")

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', re.I)
# ══════════════════════════════════════════════════════════════
# DATACLASS
# ══════════════════════════════════════════════════════════════

@dataclass
class BulkOrderLine:
    raw_fragment: str
    company_name: str                     # as typed (display only; not used for resolution)
    email: str                            # customer identifier; empty string if not provided
    product_name: str
    quantity: int
    quantity_explicitly_set: bool
    product_id: Optional[int]
    variation_id: Optional[int]
    customer_id: Optional[str]
    customer_display_name: str
    shipping_address: Optional[dict]
    billing_address: Optional[dict]
    is_reorder: bool
    reorder_source_order_id: Optional[int]
    unresolved: bool
    unresolved_reason: Optional[str]

# ══════════════════════════════════════════════════════════════
# INTERNAL: intermediate pre-line structure
# ══════════════════════════════════════════════════════════════

@dataclass
class _PreLine:
    raw_fragment: str
    company_name: str
    email: str
    product_name: str
    quantity: int
    is_reorder: bool
    quantity_explicitly_set: bool = False
    product_id: Optional[int] = None
    customer_id: Optional[str] = None
    reorder_source_order_id: Optional[int] = None
    variant_hint: str = ""
    variation_id: Optional[int] = None


# ══════════════════════════════════════════════════════════════
# PUBLIC FUNCTION
# ══════════════════════════════════════════════════════════════

def parse_bulk_order_utterance(
    text: str,
    store_loader,
    role: str = "",
    self_customer_id: Optional[str] = None,
) -> List[BulkOrderLine]:
    """
    Parse a free-text bulk order utterance into a list of resolved BulkOrderLine objects.

    Steps:
      1. Split into fragments (comma / " and <digit>")
      2. Per-fragment extraction (qty, company, product, reorder flag)
      3. Product resolution (store_loader, no API call)
      4. Company resolution (one API call per unique company, batched; rep only)
      5. Reorder resolution (API call per reorder line with resolved customer)
      6. Assemble final BulkOrderLine objects
    """
    _is_rep = role in BULK_ORDER_ROLES

    # ── Step 1: Split into fragments ─────────────────────────────────────────
    _catalog_names = {
        p["name"].lower() for p in (store_loader.products or []) if p.get("name")
    }

    # Pass 1: split on commas
    raw_parts = re.split(r',\s*', text)

    # Pass 2: within each comma-part, split on "and" if both sides
    # resolve to catalog products OR if "and" precedes a digit (existing logic).
    # This handles: "A and B", "A, B and C", "A and B and C".
    final_fragments = []
    for part in raw_parts:
        # Always split "and" before a digit (original behaviour)
        digit_split = re.split(r'\s+and\s+(?=\d)', part)
        expanded = []
        for sub in digit_split:
            if re.search(r'\band\b', sub, re.I):
                and_parts = [p.strip() for p in re.split(r'\s+and\s+', sub, flags=re.I) if p.strip()]
                resolved = sum(
                    1 for p in and_parts
                    if any(name in p.lower() for name in _catalog_names)
                )
                if resolved >= 2:
                    expanded.extend(and_parts)
                    continue
            expanded.append(sub)
        final_fragments.extend(expanded)

    # ── Step 2: Per-fragment extraction ──────────────────────────────────────
    pre_lines: List[_PreLine] = []

    for fragment in final_fragments:
        fragment = fragment.strip()
        if not fragment:
            continue

        qty_match = re.search(r'\b(\d+)\b', fragment)
        quantity = int(qty_match.group(1)) if qty_match else 1
        quantity_explicitly_set = qty_match is not None

        is_reorder = bool(
            re.search(r'\b(reorder|re-order|last\s+week[\'s]*|previous)\b', fragment, re.I)
        )

        # ── Email extraction (rep only — non-rep orders on their own account) ──
        email = ""
        if _is_rep:
            email_match = EMAIL_RE.search(fragment)
            if email_match:
                email = email_match.group(0).strip()

        # ── Company name: text after "for" minus any email (display only) ──
        company_name = ""
        if _is_rep:
            for_match = re.search(r'\bfor\s+(.+)$', fragment, re.I)
            if for_match:
                candidate = EMAIL_RE.sub('', for_match.group(1)).strip().strip(', ')
                if candidate:
                    company_name = candidate

        # ── Product: strip quantity, email addresses, and "for …" tail ──
        product_part = fragment
        if qty_match:
            product_part = product_part[qty_match.end():].strip()
        product_part = EMAIL_RE.sub('', product_part).strip()
        product_part = re.sub(r'\s*\bfor\b.*$', '', product_part, flags=re.I).strip()
        
        # Strip leading intent phrase + order verb — anchored so it never
        # strips "order" appearing mid-string (e.g. "harmony order confirmation")
        product_part = re.sub(
            r'^(?:(?:i\s+(?:want|need|would\s+like)\s+to|please|can\s+you)\s+)?'
            r'(?:order|buy|purchase|reorder|re-order)\s+',
            '',
            product_part,
            flags=re.I,
        ).strip()
        product_name = product_part.strip(" ,.-")

        pre_lines.append(_PreLine(
            raw_fragment=fragment,
            company_name=company_name,
            email=email,
            product_name=product_name,
            quantity=quantity,
            quantity_explicitly_set=quantity_explicitly_set,
            is_reorder=is_reorder,
        ))

    if not pre_lines:
        return []

    # ── Step 3: Product resolution + variant hint extraction ──────────────────
    for pl in pre_lines:
        if not store_loader or not pl.product_name:
            continue

        products = store_loader.products or []
        matched_catalog_name = None

        # 3a. Exact match (case-insensitive) — full product_name
        for p in products:
            if p.get("name", "").lower() == pl.product_name.lower():
                pl.product_id = p["id"]
                matched_catalog_name = p["name"]
                break

        # 3b. First-word exact match: "Harmony White" → try "Harmony"
        if pl.product_id is None:
            first_word = pl.product_name.split()[0] if pl.product_name.split() else ""
            if first_word:
                for p in products:
                    if p.get("name", "").lower() == first_word.lower():
                        pl.product_id = p["id"]
                        matched_catalog_name = p["name"]
                        break

        # 3b.5. Substring scan: find the longest catalog name contained within
        # product_name. Safety net for any verb-prefixed text that slipped
        # through Step 2 cleaning (e.g. "i want to order saga" → "Saga").
        if pl.product_id is None:
            for p in sorted(products, key=lambda x: len(x.get("name", "")), reverse=True):
                p_name = p.get("name", "")
                if p_name and re.search(r'\b' + re.escape(p_name) + r'\b', pl.product_name, re.I):
                    pl.product_id = p["id"]
                    matched_catalog_name = p["name"]
                    break

        # 3c. Fuzzy fallback (cutoff=0.6)
        if pl.product_id is None:
            product_names = [p.get("name", "") for p in products]
            matches = difflib.get_close_matches(
                pl.product_name, product_names, n=1, cutoff=0.6
            )
            if matches:
                matched_catalog_name = matches[0]
                pl.product_id = next(
                    (p["id"] for p in products if p.get("name") == matched_catalog_name),
                    None,
                )

        # 3d. Extract variant hint OR company name from remainder
        # 3d. Extract variant hint OR display company from remainder
        if matched_catalog_name and pl.product_name.lower().startswith(matched_catalog_name.lower()):
            remainder = pl.product_name[len(matched_catalog_name):].strip(" ,.-")
            if remainder:
                _for_match = re.match(r'^for\s+(.+)$', remainder, re.I)
                if _for_match:
                    # "for <Name>" in remainder → display-only company hint
                    # Email (the actual resolution key) was already extracted in Step 2.
                    if not pl.company_name:
                        candidate = EMAIL_RE.sub('', _for_match.group(1)).strip().strip(', ')
                        if candidate:
                            pl.company_name = candidate
                else:
                    pl.variant_hint = remainder
            pl.product_name = matched_catalog_name
            
        if pl.product_id:
            logger.debug(
                f"bulk_parser | resolved product '{pl.product_name}' → id={pl.product_id} "
                f"variant_hint='{pl.variant_hint}'"
            )
        else:
            logger.debug(
                f"bulk_parser | unresolved product '{pl.product_name}'"
            )
            
    # ── Step 3.5: Variation resolution (API call per unique product with a hint) ─
    _variant_cache: dict = {}   # product_id → list[variation dicts]; avoids duplicate calls

    for pl in pre_lines:
        if not pl.product_id or not pl.variant_hint:
            continue

        if pl.product_id not in _variant_cache:
            var_call = endpoints.list_variants(
                product_id=pl.product_id,
                per_page=100,
                description=f"Fetch variations for product_id={pl.product_id}",
            )
            var_result = woo_client.execute(var_call)
            data = var_result.get("data", [])
            _variant_cache[pl.product_id] = data if isinstance(data, list) else []

        hint_lower = pl.variant_hint.lower()
        for var in _variant_cache[pl.product_id]:
            for attr in var.get("attributes", []):
                if hint_lower in attr.get("option", "").lower():
                    pl.variation_id = var["id"]
                    break
            if pl.variation_id:
                break

        if pl.variation_id:
            logger.debug(
                f"bulk_parser | resolved variation hint='{pl.variant_hint}' "
                f"→ variation_id={pl.variation_id}"
            )
        else:
            logger.debug(
                f"bulk_parser | unresolved variation hint='{pl.variant_hint}' "
                f"for product_id={pl.product_id}"
            )

    # ── Step 4: Customer resolution by email (rep only, batched) ──────────────
    # Non-rep users have no email values, so unique_emails is empty
    # and this step is a no-op for them.
    email_resolution_cache: dict = {}   # email -> resolved dict | None

    unique_emails = list({pl.email for pl in pre_lines if pl.email})

    for email in unique_emails:
        call = endpoints.search_customers_by_email(
            email=email,
            per_page=1,
            description=f"Bulk order email lookup: '{email}'",
        )
        result = woo_client.execute(call)

        customers = result.get("data", [])
        if not result.get("success") or not isinstance(customers, list) or not customers:
            logger.debug(f"bulk_parser | email '{email}' → not found")
            email_resolution_cache[email] = None
            continue

        customer = customers[0]
        company_field = customer.get("company") or customer.get("billing", {}).get("company", "")
        full_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        display = company_field or full_name or f"Customer #{customer['id']}"

        email_resolution_cache[email] = {
            "id": str(customer["id"]),
            "display": display,
            "billing": customer.get("billing", {}),
            "shipping": customer.get("shipping", {}),
        }
        logger.debug(f"bulk_parser | email '{email}' → id={customer['id']} display='{display}'")

    # Stamp customer_id onto pre_lines so Step 5 (reorder) can use it
    for pl in pre_lines:
        if not pl.email:
            continue
        resolution = email_resolution_cache.get(pl.email)
        if resolution:
            pl.customer_id = resolution["id"]

    # ── Step 5: Reorder resolution ────────────────────────────────────────────
    for pl in pre_lines:
        if not pl.is_reorder or not pl.customer_id:
            continue

        call = endpoints.list_rep_orders(
            body={"customer_id": pl.customer_id, "per_page": 3},
            description="Fetch recent orders for reorder resolution",
        )
        result = woo_client.execute(call)

        orders = result.get("data", [])
        if isinstance(orders, dict):
            orders = orders.get("orders", [])

        source_order_id = None
        for order in orders:
            for item in order.get("line_items", []):
                pid_match = (
                    pl.product_id and item.get("product_id") == pl.product_id
                )
                name_match = (
                    pl.product_name.lower() in item.get("name", "").lower()
                )
                if pid_match or name_match:
                    source_order_id = order["id"]
                    # Backfill product_id from order history if still unresolved
                    if not pl.product_id:
                        pl.product_id = item.get("product_id")
                    break
            if source_order_id:
                break

        pl.reorder_source_order_id = source_order_id
        logger.debug(
            f"bulk_parser | reorder for customer_id={pl.customer_id} "
            f"product='{pl.product_name}' → source_order_id={source_order_id}"
        )

    # ── Step 6: Assemble final BulkOrderLine objects ──────────────────────────
    result_lines: List[BulkOrderLine] = []

    for pl in pre_lines:
        if _is_rep:
            resolution = email_resolution_cache.get(pl.email) if pl.email else None
            if resolution:
                customer_id = resolution["id"]
                customer_display_name = resolution["display"]
                shipping_address = resolution.get("shipping") or {}
                billing_address = resolution.get("billing") or {}
                if not shipping_address.get("address_1"):
                    shipping_address = billing_address
            else:
                customer_id = None
                customer_display_name = "⚠️ Not found" if pl.email else "⚠️ Email required"
                shipping_address = None
                billing_address = None
        else:
            customer_id = self_customer_id
            customer_display_name = "My Account"
            shipping_address = None
            billing_address = None

        # Unresolved reason — now distinguishes "not provided" from "not found"
        _customer_unresolved = customer_id is None
        _product_unresolved = pl.product_id is None

        if _product_unresolved and _customer_unresolved:
            unresolved = True
            unresolved_reason = "both_not_found"
        elif _product_unresolved:
            unresolved = True
            unresolved_reason = "product_not_found"
        elif _customer_unresolved:
            unresolved = True
            unresolved_reason = "email_not_provided" if (_is_rep and not pl.email) else "email_not_found"
        else:
            unresolved = False
            unresolved_reason = None

        result_lines.append(BulkOrderLine(
            raw_fragment=pl.raw_fragment,
            company_name=pl.company_name,
            email=pl.email,
            product_name=pl.product_name,
            quantity=pl.quantity,
            quantity_explicitly_set=pl.quantity_explicitly_set,
            product_id=pl.product_id,
            variation_id=pl.variation_id,
            customer_id=customer_id,
            customer_display_name=customer_display_name,
            shipping_address=shipping_address,
            billing_address=billing_address,
            is_reorder=pl.is_reorder,
            reorder_source_order_id=pl.reorder_source_order_id,
            unresolved=unresolved,
            unresolved_reason=unresolved_reason,
        ))

    logger.info(
        f"bulk_parser | parsed {len(result_lines)} lines | "
        f"unresolved={sum(1 for l in result_lines if l.unresolved)}"
    )
    return result_lines