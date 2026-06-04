"""
routes/sales_rep.py — Sales rep REST endpoints.

GET /sales-rep/recent-products
    Returns recently ordered products for a customer with reorder hints.
"""

import time
from flask import Blueprint, request, jsonify

from woo_client import woo_client
from ecommerce import endpoints
from utils.reorder_analysis import analyse_reorder_patterns
from chat_logger import get_logger

logger = get_logger("miraq_chat")
sales_rep_bp = Blueprint("sales_rep", __name__)


@sales_rep_bp.route("/sales-rep/recent-products", methods=["GET"])
def recent_products():
    t_start = time.time()

    # Step 1: Validate customer_id
    customer_id = request.args.get("customer_id")
    if not customer_id:
        return jsonify({"success": False, "products": [], "error": "customer_id is required"}), 400

    try:
        customer_id = int(customer_id)
    except (ValueError, TypeError):
        return jsonify({"success": False, "products": [], "error": "customer_id must be an integer"}), 400

    # limit: optional, default 10, max 20
    try:
        limit = min(int(request.args.get("limit", 10)), 20)
    except (ValueError, TypeError):
        limit = 10

    # Step 2: Fetch recent orders
    api_call = endpoints.list_customer_orders(
        customer_id=customer_id,
        page=1,
        per_page=20,
        description="Fetch recent orders for recently-ordered panel",
    )
    result = woo_client.execute(api_call)

    # Step 3: Validate result
    if not result.get("success") or not isinstance(result.get("data"), list):
        logger.warning(f"recent_products | failed to fetch orders for customer_id={customer_id} | error={result.get('error')}")
        return jsonify({
            "success": False,
            "products": [],
            "error": result.get("error", "Failed to fetch orders"),
        }), 200

    # Step 4: Extract orders list
    orders = result["data"]

    # Step 5: Analyse reorder patterns
    patterns = analyse_reorder_patterns(orders)

    # Step 6: Build unique product list (preserve API desc order — most recent first)
    seen_ids = set()
    products = []

    for order in orders:
        if not isinstance(order, dict):
            continue
        for item in order.get("line_items", []):
            if not isinstance(item, dict):
                continue
            product_id = item.get("product_id")
            if not product_id or product_id in seen_ids:
                continue
            seen_ids.add(product_id)
            products.append({
                "product_id": product_id,
                "product_name": item.get("name", ""),
                "quantity": item.get("quantity"),
                "last_order_id": order.get("id"),
                "last_ordered_date": (order.get("date_created") or "")[:10],
            })
            if len(products) >= limit:
                break
        if len(products) >= limit:
            break

    # Step 7: Merge reorder patterns
    for product in products:
        pattern = patterns.get(product["product_id"])
        if pattern:
            product["reorder_hint"] = pattern.hint
            product["overdue"] = pattern.overdue
            product["avg_interval_days"] = pattern.avg_interval_days
            product["days_since_last_order"] = pattern.days_since_last_order
        else:
            product["reorder_hint"] = None
            product["overdue"] = False
            product["avg_interval_days"] = None
            product["days_since_last_order"] = None

    # Step 8: Return
    logger.info(
        f"recent_products | customer_id={customer_id} | "
        f"products={len(products)} | time_ms={round((time.time() - t_start) * 1000)}"
    )
    return jsonify({
        "success": True,
        "products": products,
        "total": len(products),
    }), 200