"""
Order Service

Handles order creation logic including reorders and quick orders.
"""

from typing import Dict, Optional

from models import WooAPICall
from config.settings import (
    WOO_BASE_URL,
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
)
from chat_logger import get_logger

logger = get_logger("miraq_chat")


def create_flow_confirmed_order(
    woo_client,
    customer_id: int,
    pending_product_id: int,
    pending_product_name: str,
    pending_quantity: int,
) -> Optional[Dict]:
    """
    Create an order from flow confirmation context.
    
    Returns the created order data or None if creation failed.
    """
    logger.info(f"Creating flow-confirmed order | product_id={pending_product_id} | quantity={pending_quantity}")
    
    order_call = WooAPICall(
        method="POST",
        endpoint=f"{WOO_BASE_URL}/orders",
        params={},
        body={
            "status": "processing",
            "customer_id": customer_id,
            "payment_method": DEFAULT_PAYMENT_METHOD,
            "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
            "set_paid": False,
            "line_items": [{"product_id": pending_product_id, "quantity": pending_quantity}],
        },
        description=f"Create order for '{pending_product_name}' (confirmed via flow)",
    )
    order_resp = woo_client.execute(order_call)
    
    if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
        created_order = order_resp["data"]
        order_number = created_order.get("number") or created_order.get("id", "N/A")
        logger.info(f"Flow-confirmed order created successfully | order_id={created_order.get('id')} | order_number={order_number}")
        return created_order
    else:
        error_msg = str(order_resp.get('error', 'Unknown'))
        logger.error(f"Flow-confirmed order creation failed | error={error_msg}")
        return None


def create_reorder(
    woo_client,
    customer_id: int,
    source_order: Dict,
) -> Optional[Dict]:
    """
    Create a reorder from a source order's line items.
    
    Returns the created order data or None if creation failed.
    """
    source_line_items = source_order.get("line_items", [])
    logger.info(f"Reorder attempt | source_order_id={source_order.get('id')} | line_items_count={len(source_line_items)}")
    
    if not source_line_items or not customer_id:
        logger.warning("Reorder skipped: no line items or customer ID")
        return None
    
    new_line_items = [
        {
            "product_id": item["product_id"],
            "quantity": item.get("quantity", 1),
            **({"variation_id": item["variation_id"]} if item.get("variation_id") else {}),
        }
        for item in source_line_items
        if item.get("product_id")
    ]
    
    if not new_line_items:
        logger.warning("Reorder skipped: no valid line items")
        return None
    
    reorder_call = WooAPICall(
        method="POST",
        endpoint=f"{WOO_BASE_URL}/orders",
        params={},
        body={
            "status": "processing",
            "customer_id": customer_id,
            "payment_method": DEFAULT_PAYMENT_METHOD,
            "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
            "set_paid": False,
            "line_items": new_line_items,
        },
        description="Create reorder from last order line items (COD, on-hold)",
    )
    reorder_resp = woo_client.execute(reorder_call)
    
    if reorder_resp.get("success") and isinstance(reorder_resp.get("data"), dict):
        new_order = reorder_resp["data"]
        logger.info(f"Reorder created successfully | order_id={new_order.get('id')} | order_number={new_order.get('number')}")
        return new_order
    else:
        error_msg = str(reorder_resp.get('error', 'Unknown'))
        logger.warning(f"Reorder failed | error={error_msg}")
        return None


def create_quick_order(
    woo_client,
    customer_id: int,
    product_id: int,
    product_name: str,
    quantity: int,
) -> Optional[Dict]:
    """
    Create a quick order for a specific product.
    
    Returns the created order data or None if creation failed.
    """
    logger.info(f"Creating quick order | product_id={product_id} | quantity={quantity} | customer_id={customer_id}")
    
    order_call = WooAPICall(
        method="POST",
        endpoint=f"{WOO_BASE_URL}/orders",
        params={},
        body={
            "status": "processing",
            "customer_id": customer_id,
            "payment_method": DEFAULT_PAYMENT_METHOD,
            "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
            "set_paid": False,
            "line_items": [{"product_id": product_id, "quantity": quantity}],
        },
        description=f"Create order for product '{product_name}' (COD, processing)",
    )
    order_resp = woo_client.execute(order_call)
    
    if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
        created_order = order_resp["data"]
        line_items_summary = [
            f"{item.get('name', 'Unknown')} x{item.get('quantity', 1)}"
            for item in created_order.get("line_items", [])
        ]
        logger.info(
            f"Quick order created | order_id={created_order.get('id')} | "
            f"order_number={created_order.get('number')} | total=${created_order.get('total', '0.00')} | "
            f"line_items={line_items_summary}"
        )
        return created_order
    else:
        error_msg = str(order_resp.get('error', 'Unknown'))
        logger.error(f"Quick order creation failed | error={error_msg}")
        return None
