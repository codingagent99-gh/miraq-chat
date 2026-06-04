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


# ══════════════════════════════════════════════════════════════
# DATACLASS
# ══════════════════════════════════════════════════════════════

@dataclass
class BulkOrderLine:
    raw_fragment: str
    company_name: str                     # as typed by rep; empty string for non-rep
    product_name: str                     # as typed
    quantity: int
    product_id: Optional[int]             # resolved via store_loader; None if not found
    variation_id: Optional[int]           # resolved from product variants; None if simple product
    customer_id: Optional[str]            # resolved via WooCommerce; None if not found
    customer_display_name: str            # "ABC Builders" / "⚠️ ABC (closest)" / "⚠️ Not found"
    shipping_address: Optional[dict]      # from company's WooCommerce shipping block; None until fetched
    billing_address: Optional[dict]       # from company's WooCommerce billing block; None until fetched
    is_reorder: bool
    reorder_source_order_id: Optional[int]
    unresolved: bool                      # True if product_id or customer_id is None
    unresolved_reason: Optional[str]      # "product_not_found"|"company_not_found"|"both_not_found"


# ══════════════════════════════════════════════════════════════
# INTERNAL: intermediate pre-line structure
# ══════════════════════════════════════════════════════════════

@dataclass
class _PreLine:
    raw_fragment: str
    company_name: str
    product_name: str
    quantity: int
    is_reorder: bool
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
    raw_parts = re.split(r',\s*', text)
    final_fragments = []
    for part in raw_parts:
        # Split on " and " only when immediately followed by a digit
        sub = re.split(r'\s+and\s+(?=\d)', part)
        final_fragments.extend(sub)

    # ── Step 2: Per-fragment extraction ──────────────────────────────────────
    pre_lines: List[_PreLine] = []

    for fragment in final_fragments:
        fragment = fragment.strip()
        if not fragment:
            continue

        # Quantity: first integer in fragment
        qty_match = re.search(r'\b(\d+)\b', fragment)
        quantity = int(qty_match.group(1)) if qty_match else 1

        # Reorder flag
        is_reorder = bool(
            re.search(r'\b(reorder|re-order|last\s+week[\'s]*|previous)\b', fragment, re.I)
        )

        # Company: text after " for " (rep only)
        company_name = ""
        if _is_rep:
            for_match = re.search(r'\bfor\s+(.+)$', fragment, re.I)
            if for_match:
                company_name = for_match.group(1).strip()

        # Product: strip quantity and "for <company>" from the fragment
        product_part = fragment
        if qty_match:
            product_part = product_part[qty_match.end():].strip()
        if company_name:
            product_part = re.sub(
                r'\s*\bfor\s+' + re.escape(company_name) + r'\s*$',
                '',
                product_part,
                flags=re.I,
            ).strip()
        product_name = product_part.strip(" ,.-")

        pre_lines.append(_PreLine(
            raw_fragment=fragment,
            company_name=company_name,
            product_name=product_name,
            quantity=quantity,
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
        if matched_catalog_name and pl.product_name.lower().startswith(matched_catalog_name.lower()):
            remainder = pl.product_name[len(matched_catalog_name):].strip(" ,.-")
            if remainder:
                _for_match = re.match(r'^for\s+(.+)$', remainder, re.I)
                if _for_match:
                    # "for <Name>" → company name, not a variant hint
                    if not pl.company_name:
                        pl.company_name = _for_match.group(1).strip()
                else:
                    pl.variant_hint = remainder
            pl.product_name = matched_catalog_name   # always normalize to catalog name

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

    # ── Step 4: Company resolution (rep only, batched) ────────────────────────
    # Non-rep users have no company_name values, so unique_companies is empty
    # and this step is a no-op for them.
    company_resolution_cache: dict = {}  # company_name -> resolved dict | None

    unique_companies = list({pl.company_name for pl in pre_lines if pl.company_name})

    for company in unique_companies:
        call = endpoints.search_customers_by_company(
            company_name=company,
            per_page=3,
            description=f"Bulk order company lookup: '{company}'",
        )
        result = woo_client.execute(call)

        if (
            not result.get("success")
            or not isinstance(result.get("data"), list)
            or not result["data"]
        ):
            logger.debug(f"bulk_parser | company '{company}' → not found (API miss)")
            company_resolution_cache[company] = None
            continue

        customers = result["data"]

        displays = []
        for c in customers:
            # New endpoint returns flat 'company' key, not nested billing.company
            company_field = c.get("company", "")
            full_name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
            displays.append(company_field or full_name or f"Customer #{c['id']}")
                                                           
        close = difflib.get_close_matches(
            company.lower(),
            [d.lower() for d in displays],
            n=1,
            cutoff=0.3,
        )

        if close:
            idx = [d.lower() for d in displays].index(close[0])
            chosen = customers[idx]
            is_exact = displays[idx].lower() == company.lower()
            display_prefix = "" if is_exact else "⚠️ "
            company_resolution_cache[company] = {
                "id": str(chosen["id"]),
                "display": f"{display_prefix}{displays[idx]}",
                "billing": chosen.get("billing", {}),
                "shipping": chosen.get("shipping", {}),
            }
            logger.debug(
                f"bulk_parser | company '{company}' → "
                f"id={chosen['id']} display='{displays[idx]}' exact={is_exact}"
            )
        else:
            company_resolution_cache[company] = None
            logger.debug(f"bulk_parser | company '{company}' → no fuzzy match")

    # Stamp customer_id onto pre_lines so Step 5 can use it
    for pl in pre_lines:
        if not pl.company_name:
            continue
        resolution = company_resolution_cache.get(pl.company_name)
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
            resolution = company_resolution_cache.get(pl.company_name)
            if resolution:
                customer_id = resolution["id"]
                customer_display_name = resolution["display"]
                # shipping ← company shipping block; billing ← company billing block
                shipping_address = resolution.get("shipping") or {}
                billing_address = resolution.get("billing") or {}
                # Fallback: if the company has no shipping block on file, reuse
                # billing so the order still ships somewhere sensible.
                if not shipping_address.get("address_1"):
                    shipping_address = billing_address
            else:
                customer_id = None
                customer_display_name = "⚠️ Not found"
                shipping_address = None
                billing_address = None
        else:
            # Non-rep: always place on their own account
            customer_id = self_customer_id
            customer_display_name = "My Account"
            shipping_address = None  # fetched later during address confirmation
            billing_address = None   # fetched later during address confirmation

        unresolved = (pl.product_id is None) or (customer_id is None)
        if pl.product_id is None and customer_id is None:
            unresolved_reason = "both_not_found"
        elif pl.product_id is None:
            unresolved_reason = "product_not_found"
        elif customer_id is None:
            unresolved_reason = "company_not_found"
        else:
            unresolved_reason = None

        result_lines.append(BulkOrderLine(
            raw_fragment=pl.raw_fragment,
            company_name=pl.company_name,
            product_name=pl.product_name,
            quantity=pl.quantity,
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