"""Shared state keys for the bulk-order flow.

_BULK_STATE_KEYS is cleared by the cart step and by handle_cancel_bulk_order.
It also lists keys older sessions may still carry from the removed
company/recipient/address steps, so finishing or cancelling an order cleans
them up.
"""

_BULK_STATE_KEYS = (
    "pending_bulk_lines", "bulk_current_line_index",
    "bulk_confirmed_lines", "bulk_address_overrides",
    "bulk_awaiting_address_text",
    "bulk_product_missing_indices", "bulk_product_current_pos",
    "bulk_quantity_pending_indices", "bulk_quantity_current_pos",
    "bulk_recipient_chip_page",
    "bulk_company_skipped",
)
