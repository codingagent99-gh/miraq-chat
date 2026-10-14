"""
services/whats_new.py — the welcome screen's "What's new" section.

Rule (requirements FR-3, proposed default for Q1):
  - products published since the shopper's LAST VISIT, newest first, max 6;
  - if there are none (or it is their first visit), the store's newest products;
  - only published, shop-visible, in-stock products;
  - nothing to show → empty list, and the widget hides the section.

No LLM call and no WooCommerce/Shopify call: everything comes from the
in-memory catalog (loader.products_newest_first, built with the catalog in
lookup_builder) plus one row in this store's customer_visits table.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from chat_logger import get_logger

logger = get_logger("miraq_chat")

WHATS_NEW_LIMIT = 6
# Opens closer together than this are the same visit (see models/customer_visit.py).
VISIT_GAP = timedelta(minutes=30)

MODE_SINCE_LAST_VISIT = "since_last_visit"
MODE_NEWEST = "newest"

# Fields of formatters.format_product() a welcome-screen card needs. The full
# formatted product also carries variations/attributes, which for Shopify can
# be 100 variant dicts per product — not worth sending for a thumbnail row.
_CARD_FIELDS = (
    "id", "name", "slug", "permalink", "type",
    "price", "regular_price", "sale_price", "on_sale",
    "in_stock", "purchasable", "images", "currency_symbol",
)


# ── Visit tracking ───────────────────────────────────────────────────────────

_ensured: set = set()
_ensure_lock = threading.Lock()


def _ensure_table(tenant) -> None:
    """Create customer_visits in this store's DB if missing.

    New stores get it at provisioning (routes/provisioning._per_tenant_tables);
    stores provisioned earlier get it here, on first use, once per process —
    the same pattern as channel_link.ensure_tables.
    """
    if not tenant or not tenant.db_name or tenant.db_name in _ensured:
        return
    from models import db
    from models.customer_visit import CustomerVisit
    from store_registry import get_engine_registry
    with _ensure_lock:
        if tenant.db_name in _ensured:
            return
        engine = get_engine_registry().get_engine(tenant.db_name)
        db.metadata.create_all(bind=engine, tables=[CustomerVisit.__table__], checkfirst=True)
        _ensured.add(tenant.db_name)


def record_visit(tenant, customer_id: str) -> Optional[datetime]:
    """Record this widget open and return the "new since" baseline.

    Returns previous_visit_at: when the shopper's previous visit ended, or
    None on their first visit. Never raises — a failure here returns None,
    which just means What's new falls back to the store's newest products.
    """
    if not customer_id:
        return None
    from sqlalchemy.dialects.postgresql import insert
    from models import db
    from models.customer_visit import CustomerVisit

    now = datetime.now(timezone.utc)
    try:
        _ensure_table(tenant)
        row = db.session.get(CustomerVisit, customer_id)
        if row is None:
            # ON CONFLICT: two tabs opening at once must not 500 one of them.
            db.session.execute(
                insert(CustomerVisit)
                .values(customer_id=customer_id, last_seen_at=now, previous_visit_at=None)
                .on_conflict_do_nothing(index_elements=["customer_id"])
            )
            db.session.commit()
            return None

        if now - row.last_seen_at >= VISIT_GAP:
            # A new visit starts: the old one's last open becomes the baseline.
            row.previous_visit_at = row.last_seen_at
        row.last_seen_at = now
        since = row.previous_visit_at
        db.session.commit()
        return since
    except Exception as e:
        db.session.rollback()
        logger.warning(f"whats_new: could not record visit — serving newest products | {e}")
        return None


# ── Selection ────────────────────────────────────────────────────────────────

def _card(raw: dict) -> Optional[dict]:
    """Slim product card, or None if it must not be shown (out of stock)."""
    from formatters import format_product
    fp = format_product(raw)
    if not fp.get("in_stock"):
        return None
    card = {k: fp.get(k) for k in _CARD_FIELDS}
    card["images"] = (fp.get("images") or [])[:1]
    return card


def _take(index: list, since_ts: Optional[float], limit: int) -> List[dict]:
    """First `limit` in-stock cards from the newest-first index, stopping at
    since_ts (exclusive) when given. Index is sorted, so this stops early."""
    out = []
    for ts, raw in index:
        if since_ts is not None and ts <= since_ts:
            break
        card = _card(raw)
        if card is not None:
            out.append(card)
            if len(out) >= limit:
                break
    return out


def select_whats_new(loader, since: Optional[datetime],
                     limit: int = WHATS_NEW_LIMIT) -> Tuple[List[dict], Optional[str]]:
    """(cards, mode). mode is MODE_SINCE_LAST_VISIT, MODE_NEWEST, or None when
    there is nothing to show. The widget uses mode only to pick the heading
    ("New since your last visit" vs "New in store")."""
    index = getattr(loader, "products_newest_first", None) or []
    if not index:
        return [], None

    if since is not None:
        cards = _take(index, since.timestamp(), limit)
        if cards:
            return cards, MODE_SINCE_LAST_VISIT

    cards = _take(index, None, limit)
    return (cards, MODE_NEWEST) if cards else ([], None)
