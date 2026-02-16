"""
Formats raw WooCommerce API responses into chatbot-friendly output.
Now includes order history formatting.
"""

from typing import List


def format_product_list(products: List[dict]) -> str:
    """Format a list of products for chatbot display."""
    if not products:
        return "No products found matching your criteria."

    lines = [f"Found {len(products)} product(s):\n"]
    for p in products:
        name = p.get("name", "Unknown")
        price = p.get("price", "N/A")
        slug = p.get("slug", "")
        permalink = p.get("permalink", "")
        stock = p.get("stock_status", "unknown")
        p_type = p.get("type", "simple")

        attrs = _format_attributes(p.get("attributes", []))
        tags_str = ", ".join(t["name"] for t in p.get("tags", [])[:5])

        images = p.get("images", [])
        thumb = images[0].get("thumbnail", "") if images else ""

        lines.append(f"• **{name}** (₹{price})")
        if attrs:
            lines.append(f"  {attrs}")
        if tags_str:
            lines.append(f"  Tags: {tags_str}")
        lines.append(f"  Stock: {stock} | Type: {p_type}")
        if permalink:
            lines.append(f"  🔗 {permalink}")
        lines.append("")

    return "\n".join(lines)


def format_product_detail(product: dict) -> str:
    """Format a single product's full details."""
    name = product.get("name", "Unknown")
    lines = [f"## {name}\n"]

    lines.append(f"- **SKU:** {product.get('sku', 'N/A')}")
    lines.append(f"- **Price:** ₹{product.get('price', 'N/A')}")
    lines.append(f"- **Type:** {product.get('type', 'N/A')}")
    lines.append(f"- **Stock:** {product.get('stock_status', 'N/A')}")
    lines.append(f"- **Link:** {product.get('permalink', 'N/A')}")

    for attr in product.get("attributes", []):
        if attr.get("visible"):
            options = ", ".join(attr.get("options", []))
            lines.append(f"- **{attr['name']}:** {options}")

    variations = product.get("variations", [])
    if variations:
        lines.append(f"\n**Variations:** {len(variations)} available")

    return "\n".join(lines)


def format_attribute_terms(terms: List[dict], attr_name: str) -> str:
    """Format attribute terms (e.g., list of sizes, finishes)."""
    if not terms:
        return f"No {attr_name} options found."

    names = [t.get("name", "") for t in terms]
    return f"**Available {attr_name}:** {', '.join(names)}"


def format_tags(tags: List[dict]) -> str:
    """Format tag list for catalog display."""
    if not tags:
        return "No product categories/tags found."

    lines = ["**Product Categories:**\n"]
    for tag in tags:
        count = tag.get("count", 0)
        if count > 0:
            lines.append(f"• {tag['name']} ({count} products)")
    return "\n".join(lines)


def format_order_status(order: dict) -> str:
    """Format order details."""
    return (
        f"**Order #{order.get('id', 'N/A')}**\n"
        f"- Status: {order.get('status', 'N/A')}\n"
        f"- Date: {order.get('date_created', 'N/A')}\n"
        f"- Total: ₹{order.get('total', 'N/A')}\n"
    )


def format_order_detail(order: dict) -> str:
    """
    ★ NEW: Format a complete order with line items for display.
    Used by LAST_ORDER, ORDER_HISTORY intents.
    """
    order_id = order.get("id", "N/A")
    status = order.get("status", "unknown").replace("-", " ").title()
    date = order.get("date_created", "N/A")
    total = order.get("total", "0.00")

    if "T" in str(date):
        date = date.split("T")[0]

    lines = [
        f"📦 **Order #{order_id}**",
        f"  📅 Date: {date}",
        f"  🔖 Status: {status}",
        f"  💰 Total: ₹{total}",
    ]

    line_items = order.get("line_items", [])
    if line_items:
        lines.append(f"  📋 Items:")
        for item in line_items:
            name = item.get("name", "Unknown")
            qty = item.get("quantity", 1)
            item_total = item.get("total", "0.00")
            lines.append(f"    • {name} × {qty} — ₹{item_total}")

    return "\n".join(lines)


def format_order_list(orders: List[dict]) -> str:
    """
    ★ NEW: Format multiple orders for ORDER_HISTORY display.
    """
    if not orders:
        return (
            "📭 No orders found for your account.\n\n"
            "Browse our tile collections to get started!"
        )

    lines = [f"📦 **Your Order History** ({len(orders)} orders):\n"]
    for order in orders:
        lines.append(format_order_detail(order))
        lines.append("")

    return "\n".join(lines)


def _format_attributes(attributes: List[dict]) -> str:
    """Extract key visible attributes into a short string."""
    parts = []
    for attr in attributes:
        if attr.get("visible") and attr.get("options"):
            val = ", ".join(attr["options"][:3])
            if len(attr["options"]) > 3:
                val += f" +{len(attr['options'])-3} more"
            parts.append(f"{attr['name']}: {val}")
    return " | ".join(parts[:4])