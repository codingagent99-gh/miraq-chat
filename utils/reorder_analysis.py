from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class ReorderPattern:
    product_id: int
    product_name: str
    last_ordered_date: str        # ISO string e.g. "2024-01-15T10:30:00"
    avg_interval_days: Optional[float]   # None if only one order exists
    days_since_last_order: int
    overdue: bool                 # True when days_since > avg_interval (1.0x, no buffer)
    hint: str                     # human-readable string shown in UI


def analyse_reorder_patterns(orders: list) -> dict:
    """
    Input:  list of raw WooCommerce order dicts.
            Each order has:
              - "date_created": ISO string e.g. "2024-01-15T10:30:00"
              - "line_items": list of dicts, each with:
                  "product_id": int
                  "name": str
                  "quantity": int

    Output: dict keyed by product_id (int) -> ReorderPattern
    """
    if not orders:
        return {}

    # Step 1: Build product_orders: dict[product_id -> list of (date, name)]
    product_orders: dict[int, list[tuple[datetime, str]]] = {}

    for order in orders:
        if not isinstance(order, dict):
            continue

        raw_date = order.get("date_created")
        if not raw_date:
            continue

        try:
            # Strip timezone offset if present (e.g. "+00:00" or "Z")
            parsed_date = datetime.fromisoformat(raw_date.rstrip("Z"))
            if parsed_date.tzinfo is not None:
                parsed_date = parsed_date.replace(tzinfo=None)
        except (ValueError, AttributeError):
            continue

        for item in order.get("line_items", []):
            if not isinstance(item, dict):
                continue

            product_id = item.get("product_id")
            if not product_id:  # skips None and 0
                continue

            name = item.get("name", "")
            product_orders.setdefault(product_id, []).append((parsed_date, name))

    # Step 2
    today = datetime.utcnow().date()

    # Step 3: Build a ReorderPattern for each product
    result: dict[int, ReorderPattern] = {}

    for product_id, entries in product_orders.items():
        # a. Sort ascending by date
        entries.sort(key=lambda x: x[0])

        dates = [e[0] for e in entries]

        # b/c/d
        most_recent_date = max(dates)
        days_since = (today - most_recent_date.date()).days
        product_name = entries[-1][1]  # name from most recent entry

        if len(dates) == 1:
            # e. Only one order
            avg_interval_days = None
            overdue = False
            hint = f"Ordered once — {days_since} days ago"
        else:
            # f. Two or more orders
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            avg_interval_days = round(sum(gaps) / len(gaps), 1)
            overdue = days_since > avg_interval_days
            if overdue:
                hint = f"Usually ordered every ~{int(avg_interval_days)}d — last ordered {days_since} days ago ⚠️"
            else:
                hint = f"Usually ordered every ~{int(avg_interval_days)}d — last ordered {days_since} days ago"

        # g. Store
        result[product_id] = ReorderPattern(
            product_id=product_id,
            product_name=product_name,
            last_ordered_date=most_recent_date.isoformat(),
            avg_interval_days=avg_interval_days,
            days_since_last_order=days_since,
            overdue=overdue,
            hint=hint,
        )

    return result