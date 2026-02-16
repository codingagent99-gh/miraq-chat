"""
Order Service for WGC Tiles Store
===================================
Handles order history, last order lookup, reorder, and placing new orders.

Uses WooCommerce REST API:
  - GET /orders          → list/search orders
  - GET /orders/<id>     → single order detail
  - POST /orders         → create new order
  - GET /products        → resolve product name → ID

Auth: Browser UA + query-string auth (same as store_loader.py)
"""

import os
import re
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
import requests as http_requests

load_dotenv()

WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
REQUEST_TIMEOUT = 30

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class OrderService:
    """
    Manages WooCommerce order operations:
      - get_last_order(customer_id)
      - get_order_history(customer_id, count)
      - get_order_by_id(order_id)
      - reorder_last(customer_id)
      - place_order_by_product_name(customer_id, product_name, quantity)
      - resolve_product(product_name) → product dict
    """

    def __init__(self):
        self.base = WOO_BASE_URL
        self.session = http_requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    # ─────────────────────────────────────────────
    # INTERNAL: Authenticated GET / POST
    # ─────────────────────────────────────────────

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Authenticated GET request to WooCommerce."""
        params = params or {}
        params["consumer_key"] = WOO_CONSUMER_KEY
        params["consumer_secret"] = WOO_CONSUMER_SECRET

        try:
            resp = self.session.get(
                f"{self.base}/{endpoint}",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return {
                "success": True,
                "data": resp.json(),
                "total": resp.headers.get("X-WP-Total"),
                "total_pages": resp.headers.get("X-WP-TotalPages"),
            }
        except http_requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else "N/A"
            return {
                "success": False,
                "data": None,
                "error": f"HTTP {status}: {body}",
            }
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    def _post(self, endpoint: str, body: dict, params: Optional[dict] = None) -> dict:
        """Authenticated POST request to WooCommerce."""
        auth_params = {
            "consumer_key": WOO_CONSUMER_KEY,
            "consumer_secret": WOO_CONSUMER_SECRET,
        }
        if params:
            auth_params.update(params)

        try:
            resp = self.session.post(
                f"{self.base}/{endpoint}",
                params=auth_params,
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return {"success": True, "data": resp.json()}
        except http_requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body_text = e.response.text[:300] if e.response is not None else "N/A"
            return {
                "success": False,
                "data": None,
                "error": f"HTTP {status}: {body_text}",
            }
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    # ─────────────────────────────────────────────
    # ORDER HISTORY
    # ─────────────────────────────────────────────

    def get_last_order(self, customer_id: int) -> dict:
        """
        Fetch the most recent order for a customer.

        Returns:
            {
                "success": True/False,
                "order": { formatted order } or None,
                "error": "..." (if failed)
            }
        """
        result = self._get("orders", {
            "customer": customer_id,
            "per_page": 1,
            "orderby": "date",
            "order": "desc",
        })

        if not result["success"]:
            return {
                "success": False,
                "order": None,
                "error": result.get("error", "Failed to fetch orders"),
            }

        orders = result["data"]
        if not orders or not isinstance(orders, list) or len(orders) == 0:
            return {
                "success": True,
                "order": None,
                "error": "No orders found for this customer.",
            }

        return {
            "success": True,
            "order": self._format_order(orders[0]),
        }

    def get_order_history(
        self, customer_id: int, count: int = 5
    ) -> dict:
        """
        Fetch recent order history for a customer.

        Args:
            customer_id: WooCommerce customer ID
            count: Number of orders to fetch (default 5, max 20)

        Returns:
            {
                "success": True/False,
                "orders": [ { formatted order }, ... ],
                "total": "10",
                "error": "..." (if failed)
            }
        """
        count = min(max(count, 1), 20)

        result = self._get("orders", {
            "customer": customer_id,
            "per_page": count,
            "orderby": "date",
            "order": "desc",
        })

        if not result["success"]:
            return {
                "success": False,
                "orders": [],
                "total": 0,
                "error": result.get("error", "Failed to fetch orders"),
            }

        orders = result["data"]
        if not orders or not isinstance(orders, list):
            return {
                "success": True,
                "orders": [],
                "total": 0,
                "error": "No orders found for this customer.",
            }

        return {
            "success": True,
            "orders": [self._format_order(o) for o in orders],
            "total": result.get("total", len(orders)),
        }

    def get_order_by_id(self, order_id: int) -> dict:
        """
        Fetch a single order by its ID.

        Returns:
            {
                "success": True/False,
                "order": { formatted order } or None,
                "error": "..." (if failed)
            }
        """
        result = self._get(f"orders/{order_id}")

        if not result["success"]:
            return {
                "success": False,
                "order": None,
                "error": result.get("error", f"Failed to fetch order #{order_id}"),
            }

        order_data = result["data"]
        if not order_data or not isinstance(order_data, dict):
            return {
                "success": False,
                "order": None,
                "error": f"Order #{order_id} not found.",
            }

        return {
            "success": True,
            "order": self._format_order(order_data),
        }

    # ─────────────────────────────────────────────
    # REORDER
    # ─────────────────────────────────────────────

    def reorder_last(self, customer_id: int) -> dict:
        """
        Repeat the customer's last order by creating a new order
        with the same line items.

        Returns:
            {
                "success": True/False,
                "original_order": { ... },
                "new_order": { ... } or None,
                "error": "..." (if failed)
            }
        """
        # Step 1: Get the last order
        last = self.get_last_order(customer_id)
        if not last["success"] or not last.get("order"):
            return {
                "success": False,
                "original_order": None,
                "new_order": None,
                "error": last.get("error", "No previous order found to repeat."),
            }

        original = last["order"]

        # Step 2: Build line items from the original order
        line_items = []
        for item in original.get("line_items", []):
            line = {
                "product_id": item["product_id"],
                "quantity": item["quantity"],
            }
            if item.get("variation_id"):
                line["variation_id"] = item["variation_id"]
            line_items.append(line)

        if not line_items:
            return {
                "success": False,
                "original_order": original,
                "new_order": None,
                "error": "Last order has no items to reorder.",
            }

        # Step 3: Create new order
        order_body = {
            "customer_id": customer_id,
            "status": "pending",
            "line_items": line_items,
            "set_paid": False,
        }

        # Copy billing/shipping from original if available
        if original.get("billing") and original["billing"].get("email"):
            order_body["billing"] = original["billing"]
        if original.get("shipping") and original["shipping"].get("address_1"):
            order_body["shipping"] = original["shipping"]

        result = self._post("orders", order_body)

        if not result["success"]:
            return {
                "success": False,
                "original_order": original,
                "new_order": None,
                "error": result.get("error", "Failed to create reorder."),
            }

        return {
            "success": True,
            "original_order": original,
            "new_order": self._format_order(result["data"]),
        }

    # ─────────────────────────────────────────────
    # PLACE ORDER BY PRODUCT NAME
    # ─────────────────────────────────────────────

    def resolve_product(self, product_name: str) -> Optional[dict]:
        """
        Search WooCommerce for a product by name.
        Returns the best-matching product dict, or None.
        """
        result = self._get("products", {
            "search": product_name,
            "status": "publish",
            "per_page": 5,
        })

        if not result["success"] or not result["data"]:
            return None

        products = result["data"]
        if not isinstance(products, list) or len(products) == 0:
            return None

        # Try exact name match first (case-insensitive)
        name_lower = product_name.lower().strip()
        for p in products:
            p_name = p.get("name", "").lower().strip()
            if p_name == name_lower:
                return p

        # Try starts-with match
        for p in products:
            p_name = p.get("name", "").lower().strip()
            if p_name.startswith(name_lower) or name_lower.startswith(p_name):
                return p

        # Fall back to first search result
        return products[0]

    def place_order_by_product_name(
        self,
        customer_id: int,
        product_name: str,
        quantity: int = 1,
        variation_id: Optional[int] = None,
    ) -> dict:
        """
        Place a new order by product name.

        Steps:
          1. Resolve product_name → product_id via WooCommerce search
          2. Create order with that product

        Args:
            customer_id: WooCommerce customer ID
            product_name: Human-readable product name (e.g., "Ansel", "Allspice")
            quantity: Number of units (default 1)
            variation_id: Optional specific variation ID

        Returns:
            {
                "success": True/False,
                "product": { resolved product summary } or None,
                "order": { formatted new order } or None,
                "error": "..." (if failed)
            }
        """
        # Step 1: Resolve product
        product = self.resolve_product(product_name)
        if not product:
            return {
                "success": False,
                "product": None,
                "order": None,
                "error": (
                    f"Could not find a product matching '{product_name}'. "
                    f"Please check the name and try again."
                ),
            }

        product_id = product["id"]
        product_type = product.get("type", "simple")
        product_summary = {
            "id": product_id,
            "name": product.get("name", ""),
            "slug": product.get("slug", ""),
            "price": product.get("price", "0"),
            "type": product_type,
            "in_stock": product.get("stock_status") == "instock",
            "permalink": product.get("permalink", ""),
        }

        # Check stock
        if product.get("stock_status") != "instock":
            return {
                "success": False,
                "product": product_summary,
                "order": None,
                "error": (
                    f"'{product.get('name')}' is currently out of stock. "
                    f"Please check back later or contact us."
                ),
            }

        # For variable products without a specified variation,
        # use the first available variation
        line_item = {
            "product_id": product_id,
            "quantity": max(quantity, 1),
        }

        if variation_id:
            line_item["variation_id"] = variation_id
        elif product_type == "variable":
            variations = product.get("variations", [])
            if variations:
                # Use the first variation as default
                line_item["variation_id"] = variations[0]
            else:
                return {
                    "success": False,
                    "product": product_summary,
                    "order": None,
                    "error": (
                        f"'{product.get('name')}' is a variable product but has "
                        f"no variations available. Please specify the variation."
                    ),
                }

        # Step 2: Create the order
        order_body = {
            "customer_id": customer_id,
            "status": "pending",
            "line_items": [line_item],
            "set_paid": False,
        }

        result = self._post("orders", order_body)

        if not result["success"]:
            return {
                "success": False,
                "product": product_summary,
                "order": None,
                "error": result.get("error", "Failed to create order."),
            }

        return {
            "success": True,
            "product": product_summary,
            "order": self._format_order(result["data"]),
        }

    # ─────────────────────────────────────────────
    # ORDER FORMATTING
    # ─────────────────────────────────────────────

    def _format_order(self, raw: dict) -> dict:
        """
        Convert raw WooCommerce order JSON into a clean,
        frontend-friendly format.
        """
        line_items = []
        for item in raw.get("line_items", []):
            line_items.append({
                "product_id": item.get("product_id"),
                "variation_id": item.get("variation_id"),
                "name": item.get("name", ""),
                "quantity": item.get("quantity", 0),
                "price": item.get("price", "0"),
                "subtotal": item.get("subtotal", "0"),
                "total": item.get("total", "0"),
                "sku": item.get("sku", ""),
                "image": (
                    item.get("image", {}).get("src", "")
                    if isinstance(item.get("image"), dict) else ""
                ),
            })

        billing = raw.get("billing", {})
        shipping = raw.get("shipping", {})

        return {
            "id": raw.get("id"),
            "number": raw.get("number", str(raw.get("id", ""))),
            "status": raw.get("status", "unknown"),
            "currency": raw.get("currency", "USD"),
            "total": raw.get("total", "0"),
            "subtotal": self._sum_line_totals(raw.get("line_items", [])),
            "total_tax": raw.get("total_tax", "0"),
            "shipping_total": raw.get("shipping_total", "0"),
            "discount_total": raw.get("discount_total", "0"),
            "date_created": raw.get("date_created", ""),
            "date_modified": raw.get("date_modified", ""),
            "date_completed": raw.get("date_completed"),
            "date_paid": raw.get("date_paid"),
            "customer_id": raw.get("customer_id"),
            "line_items": line_items,
            "item_count": sum(i.get("quantity", 0) for i in raw.get("line_items", [])),
            "payment_method": raw.get("payment_method", ""),
            "payment_method_title": raw.get("payment_method_title", ""),
            "billing": {
                "first_name": billing.get("first_name", ""),
                "last_name": billing.get("last_name", ""),
                "email": billing.get("email", ""),
                "phone": billing.get("phone", ""),
                "address_1": billing.get("address_1", ""),
                "city": billing.get("city", ""),
                "state": billing.get("state", ""),
                "postcode": billing.get("postcode", ""),
                "country": billing.get("country", ""),
            },
            "shipping": {
                "first_name": shipping.get("first_name", ""),
                "last_name": shipping.get("last_name", ""),
                "address_1": shipping.get("address_1", ""),
                "city": shipping.get("city", ""),
                "state": shipping.get("state", ""),
                "postcode": shipping.get("postcode", ""),
                "country": shipping.get("country", ""),
            },
            "customer_note": raw.get("customer_note", ""),
            "coupon_lines": [
                {"code": c.get("code", ""), "discount": c.get("discount", "0")}
                for c in raw.get("coupon_lines", [])
            ],
        }

    @staticmethod
    def _sum_line_totals(line_items: list) -> str:
        """Sum all line item totals."""
        total = 0.0
        for item in line_items:
            try:
                total += float(item.get("total", 0))
            except (ValueError, TypeError):
                pass
        return f"{total:.2f}"

    # ─────────────────────────────────────────────
    # BOT MESSAGE FORMATTING
    # ─────────────────────────────────────────────

    def format_last_order_message(self, order: Optional[dict]) -> str:
        """Generate a bot message for 'show my last order'."""
        if not order:
            return (
                "You don't have any previous orders yet. 📦\n\n"
                "Browse our collection and place your first order!"
            )

        msg = f"📦 **Your Last Order** (#{order['number']})\n\n"
        msg += f"**Status:** {order['status'].title()}\n"
        msg += f"**Date:** {self._format_date(order['date_created'])}\n"
        msg += f"**Total:** ${order['total']}\n\n"

        if order["line_items"]:
            msg += "**Items:**\n"
            for item in order["line_items"]:
                qty = item["quantity"]
                name = item["name"]
                price = item["total"]
                msg += f"  • {name} × {qty} — ${price}\n"

        return msg

    def format_order_history_message(self, orders: List[dict]) -> str:
        """Generate a bot message for 'show my order history'."""
        if not orders:
            return (
                "You don't have any orders yet. 📦\n\n"
                "Browse our collection and place your first order!"
            )

        msg = f"📋 **Your Order History** ({len(orders)} orders)\n\n"

        for order in orders:
            item_names = ", ".join(
                i["name"] for i in order["line_items"][:3]
            )
            if len(order["line_items"]) > 3:
                item_names += f" +{len(order['line_items']) - 3} more"

            msg += (
                f"**#{order['number']}** — {order['status'].title()} "
                f"— ${order['total']} "
                f"— {self._format_date(order['date_created'])}\n"
                f"  Items: {item_names}\n\n"
            )

        return msg

    def format_reorder_message(
        self,
        original: Optional[dict],
        new_order: Optional[dict],
        error: Optional[str] = None,
    ) -> str:
        """Generate a bot message for 'repeat my last order'."""
        if error:
            return f"❌ Could not reorder: {error}"

        if not original or not new_order:
            return "❌ Something went wrong with the reorder. Please try again."

        msg = f"✅ **Reorder Successful!**\n\n"
        msg += f"Your previous order **#{original['number']}** has been reordered.\n"
        msg += f"New order: **#{new_order['number']}**\n"
        msg += f"**Status:** {new_order['status'].title()}\n"
        msg += f"**Total:** ${new_order['total']}\n\n"

        if new_order["line_items"]:
            msg += "**Items:**\n"
            for item in new_order["line_items"]:
                msg += f"  • {item['name']} × {item['quantity']}\n"

        msg += (
            "\n💡 Your order is pending. "
            "Proceed to checkout to complete the payment."
        )
        return msg

    def format_place_order_message(
        self,
        product: Optional[dict],
        order: Optional[dict],
        error: Optional[str] = None,
    ) -> str:
        """Generate a bot message for 'order this item <name>'."""
        if error:
            return f"❌ {error}"

        if not product or not order:
            return "❌ Something went wrong placing the order. Please try again."

        msg = f"✅ **Order Placed!**\n\n"
        msg += f"**Product:** {product['name']}\n"
        msg += f"**Order #:** {order['number']}\n"
        msg += f"**Status:** {order['status'].title()}\n"
        msg += f"**Total:** ${order['total']}\n\n"

        if product.get("permalink"):
            msg += f"🔗 [View Product]({product['permalink']})\n\n"

        msg += (
            "💡 Your order is pending. "
            "Proceed to checkout to complete the payment."
        )
        return msg

    @staticmethod
    def _format_date(date_str: str) -> str:
        """Format a WooCommerce date string to readable format."""
        if not date_str:
            return "N/A"
        try:
            # WooCommerce format: "2026-02-16T05:52:55"
            from datetime import datetime
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%b %d, %Y")
        except (ValueError, TypeError):
            return date_str[:10] if len(date_str) >= 10 else date_str