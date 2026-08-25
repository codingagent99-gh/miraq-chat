"""Small shared helpers for the bulk-order flow.

Split verbatim out of handlers/bulk_order_handler.py — pure move, no logic
changes. These two are here rather than in any one feature module because
they are used from more than one of them (_get by the confirmation table and
the address group; _BULK_STATE_KEYS by the cart/confirm path and by
handle_cancel_bulk_order, which stays in the handler). Keeping them in a
dependency-free module means no feature module has to import another just to
reach them.
"""

_BULK_STATE_KEYS = (
    "pending_bulk_lines", "bulk_current_line_index",
    "bulk_confirmed_lines", "bulk_address_overrides",
    "bulk_awaiting_address_text",
    "bulk_product_missing_indices", "bulk_product_current_pos",
    "bulk_quantity_pending_indices", "bulk_quantity_current_pos",
    # Which page of contact chips the recipient prompt is showing.
    "bulk_recipient_chip_page",
    # Set when the rep answers "Continue anyway" to a company with no records.
    # It suppresses the company prompt for the REST OF THAT ORDER only — left
    # behind, it would silently skip company resolution on the next bulk order
    # in the same session.
    "bulk_company_skipped",
)

# ══════════════════════════════════════════════════════════════
# ── Helper: dual-access for BulkOrderLine or dict ──
# ══════════════════════════════════════════════════════════════

def _get(line, key, default=None):
    """Read a field from either a BulkOrderLine dataclass or a plain dict."""
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)