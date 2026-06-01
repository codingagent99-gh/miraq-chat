"""
api_builder/shopify_orders_executor.py — Shopify Admin GraphQL order fetcher.

Translates WooAPICall bodies (built by shopify_order_calls.py) into Shopify
Admin GraphQL requests and returns orders normalised to the WooCommerce shape
that every downstream handler and formatter already expects.

Normalised order shape (mirrors WooCommerce REST response):
    {
        "id":                str,   # Shopify GID, e.g. "gid://shopify/Order/5001"
        "number":            str,   # Shopify order name, e.g. "#1001"
        "status":            str,   # "pending" | "processing" | "completed" | …
        "total":             str,
        "subtotal":          str,
        "shipping_total":    str,
        "date_created":      str,   # ISO-8601
        "date_paid":         str | None,
        "payment_method":    str,
        "currency_symbol":   str,
        "customer_id":       str,   # Shopify customer GID
        "line_items": [
            {
                "name":         str,
                "quantity":     int,
                "price":        str,
                "total":        str,
                "sku":          str,
                "product_id":   str,   # Shopify product GID
                "variation_id": str,   # Shopify variant GID
            }
        ],
        "shipping": {
            "first_name": str, "last_name": str,
            "address_1":  str, "address_2": str,
            "city":       str, "state":     str,
            "postcode":   str, "country":   str,
        },
        "billing":  { … same shape … },
        "_raw":     dict,   # original GraphQL node for forward-compat access
    }

execute() return envelope:
    {
        "orders": list[dict],
        "total":  int,
        "pages":  int,
        "page":   int,
    }
"""

import time
from typing import Optional

import requests as http_requests

from chat_logger import get_logger
from models.shopify_token import ShopifyToken
from store_loader.config import SHOPIFY_STORE_DOMAIN

logger = get_logger("miraq_chat")

API_VERSION = "2024-10"

# ══════════════════════════════════════════════════════════════
# Currency code → symbol map  (mirrors shopify_fetcher.py)
# ══════════════════════════════════════════════════════════════

_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
    "CAD": "C$", "AUD": "A$", "JPY": "¥",
}


# ══════════════════════════════════════════════════════════════
# GraphQL queries
# ══════════════════════════════════════════════════════════════

_CUSTOMER_ADDRESS_GQL = """
query CustomerAddress($customer_gid: ID!) {
  customer(id: $customer_gid) {
    defaultAddress {
      firstName lastName
      address1 address2
      city province zip country
      phone
    }
  }
}
"""

# List orders for a customer, optionally filtered by date range.
# Variables: customer_gid (String!), first (Int!), after (String),
#            query_filter (String)  — appended to the orders query string
_CUSTOMER_ORDERS_GQL = """
query CustomerOrders($customer_gid: ID!, $first: Int!, $after: String) {
  customer(id: $customer_gid) {
    orders(first: $first, after: $after, sortKey: PROCESSED_AT, reverse: true) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          name
          displayFinancialStatus
          displayFulfillmentStatus
          processedAt
          currencyCode
          totalPriceSet         { shopMoney { amount } }
          subtotalPriceSet      { shopMoney { amount } }
          totalShippingPriceSet { shopMoney { amount } }
          paymentGatewayNames
          customer { id }
          shippingAddress {
            firstName lastName
            address1 address2
            city province zip country
          }
          lineItems(first: 50) {
            edges {
              node {
                title
                quantity
                originalUnitPriceSet { shopMoney { amount } }
                discountedTotalSet   { shopMoney { amount } }
                sku
                product  { id }
                variant  { id }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Fetch a single order by its GID.
_FETCH_ORDER_GQL = """
query FetchOrder($order_gid: ID!) {
  order(id: $order_gid) {
    id
    name
    displayFinancialStatus
    displayFulfillmentStatus
    processedAt
    currencyCode
    totalPriceSet         { shopMoney { amount } }
    subtotalPriceSet      { shopMoney { amount } }
    totalShippingPriceSet { shopMoney { amount } }
    paymentGatewayNames
    customer { id }
    shippingAddress {
      firstName lastName
      address1 address2
      city province zip country
    }
    lineItems(first: 50) {
      edges {
        node {
          title
          quantity
          originalUnitPriceSet { shopMoney { amount } }
          discountedTotalSet   { shopMoney { amount } }
          sku
          product  { id }
          variant  { id }
        }
      }
    }
  }
}
"""


# ══════════════════════════════════════════════════════════════
# Transport
# ══════════════════════════════════════════════════════════════

def _gql(query: str, variables: dict, token: str) -> dict:
    """Execute one GraphQL request against the Shopify Admin API."""
    resp = http_requests.post(
        f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{API_VERSION}/graphql.json",
        json={"query": query, "variables": variables},
        headers={
            "Content-Type":           "application/json",
            "X-Shopify-Access-Token": token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"Shopify GraphQL errors: {data['errors']}")
    return data["data"]


# ══════════════════════════════════════════════════════════════
# Normalisation
# ══════════════════════════════════════════════════════════════

def _money(price_set: Optional[dict]) -> str:
    """Extract a money string from a Shopify MoneyBag / MoneyV2 node."""
    if not price_set:
        return "0"
    shop_money = price_set.get("shopMoney") or {}
    return shop_money.get("amount") or "0"


def _address(addr: Optional[dict]) -> dict:
    """Normalise a Shopify MailingAddress node to the neutral 8-key shape."""
    if not addr:
        return {
            "first_name": "", "last_name": "",
            "address_1": "", "address_2": "",
            "city": "", "state": "", "postcode": "", "country": "",
        }
    return {
        "first_name": addr.get("firstName") or "",
        "last_name":  addr.get("lastName")  or "",
        "address_1":  addr.get("address1")  or "",
        "address_2":  addr.get("address2")  or "",
        "city":       addr.get("city")      or "",
        "state":      addr.get("province")  or "",
        "postcode":   addr.get("zip")       or "",
        "country":    addr.get("country")   or "",
    }


def _map_status(financial: str, fulfillment: str) -> str:
    """
    Derive a WooCommerce-style status string from Shopify's two status fields.

    Shopify displayFinancialStatus: PAID, PENDING, REFUNDED, PARTIALLY_REFUNDED,
                                    VOIDED, PARTIALLY_PAID, EXPIRED
    Shopify displayFulfillmentStatus: FULFILLED, IN_PROGRESS, ON_HOLD,
                                      OPEN, PARTIALLY_FULFILLED, PENDING_FULFILLMENT,
                                      RESTOCKED, SCHEDULED, UNFULFILLED
    """
    fin  = (financial   or "").upper()
    ful  = (fulfillment or "").upper()

    if fin == "REFUNDED":
        return "refunded"
    if fin == "PARTIALLY_REFUNDED":
        return "refunded"
    if fin == "VOIDED":
        return "cancelled"
    if ful == "FULFILLED" and fin == "PAID":
        return "completed"
    if fin == "PAID" and ful in ("UNFULFILLED", "OPEN", "IN_PROGRESS",
                                  "PARTIALLY_FULFILLED", "PENDING_FULFILLMENT"):
        return "processing"
    if fin == "PENDING":
        return "pending"
    if fin == "PARTIALLY_PAID":
        return "on-hold"

    # Reasonable fallback
    return (fin.lower().replace("_", "-") or "processing")


def _normalise_order(node: dict) -> dict:
    """
    Shopify Admin GraphQL order node → WooCommerce-shaped order dict.

    All downstream code (format_order_for_frontend, handle_reorder,
    handle_historical_search, format_order_detail, response_generator)
    reads the WooCommerce shape, so we map here once and never touch
    those functions.
    """
    currency_code   = node.get("currencyCode") or "USD"
    currency_symbol = _CURRENCY_SYMBOLS.get(currency_code, currency_code)

    financial   = node.get("displayFinancialStatus")   or ""
    fulfillment = node.get("displayFulfillmentStatus") or ""
    status      = _map_status(financial, fulfillment)

    # Line items
    line_item_edges = (node.get("lineItems") or {}).get("edges") or []
    line_items = []
    for edge in line_item_edges:
        li = edge.get("node") or {}
        qty   = li.get("quantity") or 1
        price = _money(li.get("originalUnitPriceSet"))
        total = _money(li.get("discountedTotalSet"))
        line_items.append({
            "name":         li.get("title") or "",
            "quantity":     int(qty),
            "price":        price,
            "total":        total,
            "sku":          li.get("sku") or "",
            # Shopify GIDs — handlers that need numeric IDs (e.g. WooCommerce
            # stock checks) won't reach this path, so GIDs are fine here.
            "product_id":   (li.get("product")  or {}).get("id") or "",
            "variation_id": (li.get("variant")   or {}).get("id") or "",
        })

    shipping_addr = _address(node.get("shippingAddress"))

    customer_node = node.get("customer") or {}
    customer_id   = customer_node.get("id") or ""

    # Shopify orders are paid at processedAt when financial status is PAID
    paid_at = node.get("processedAt") if financial.upper() == "PAID" else None

    payment_gateways = node.get("paymentGatewayNames") or []
    payment_method   = payment_gateways[0] if payment_gateways else ""

    return {
        "id":             node.get("id") or "",
        "number":         (node.get("name") or "").lstrip("#"),   # "1001"
        "status":         status,
        "total":          _money(node.get("totalPriceSet")),
        "subtotal":       _money(node.get("subtotalPriceSet")),
        "shipping_total": _money(node.get("totalShippingPriceSet")),
        "date_created":   node.get("processedAt") or "",
        "date_paid":      paid_at,
        "payment_method": payment_method,
        # format_order_for_frontend checks currency_symbol key
        "currency_symbol": currency_symbol,
        # handle_reorder security check reads customer_id
        "customer_id":    customer_id,
        "line_items":     line_items,
        "shipping":       shipping_addr,
        # Shopify has one address; mirror it to billing so WooCommerce
        # formatters that read billing don't get an empty dict
        "billing":        dict(shipping_addr),
        "_raw":           node,
    }


# ══════════════════════════════════════════════════════════════
# Executor
# ══════════════════════════════════════════════════════════════

class ShopifyOrdersExecutor:
    """
    Execute a WooAPICall (built by shopify_order_calls.py) against the
    Shopify Admin GraphQL orders API.

    Usage in chat.py (_execute_api_calls):

        from api_builder.shopify_orders_executor import ShopifyOrdersExecutor
        executor = ShopifyOrdersExecutor()
        result = executor.execute(call)
        # result: {"orders": [...], "total": N, "pages": N, "page": N}
    """

    # ── public ───────────────────────────────────────────────

    def execute(self, call) -> dict:
        """
        Dispatch to the appropriate query based on call.body["_op"].

        Supported operations (set by shopify_order_calls.py):
            "list_customer_orders"  — paginated order list for a customer
            "fetch_order"           — single order by GID or numeric id
        """
        t0   = time.time()
        body = call.body or {}
        op   = body.get("_op", "list_customer_orders")

        logger.info(
            f"[ShopifyOrders] execute | op={op} "
            f"customer={body.get('customer_gid')!r} "
            f"order_id={body.get('order_id')!r} "
            f"page={body.get('page')} per_page={body.get('per_page')}"
        )

        token = self._get_token()

        # Resolve CURRENT_USER_ID placeholder.
        # _resolve_user_placeholders in response_generator.py handles WooCommerce
        # params but does not walk body["customer_gid"].  The order builders in
        # __init__.py call _customer_gid("CURRENT_USER_ID") when customer_id has
        # not been resolved yet; by the time execute() is called, chat.py has
        # already run _resolve_user_placeholders which walks call.params and
        # call.endpoint — it does not touch call.body for shopify_orders calls.
        # We therefore do the substitution here: if the placeholder is still
        # present the customer is not logged in and we return empty gracefully.
        if (op == "list_customer_orders"
                and body.get("customer_gid") == "CURRENT_USER_ID"):
            logger.warning(
                "[ShopifyOrders] customer_gid still CURRENT_USER_ID at execute time — "
                "customer not logged in or placeholder not resolved"
            )
            return self._empty_result(body)

        if op == "fetch_order":
            result = self._fetch_single_order(body, token)
        elif op == "create_order":
            result = self._create_order(body, token)
        else:
            result = self._list_customer_orders(body, token)
            
        logger.info(
            f"[ShopifyOrders] done | op={op} "
            f"orders={result.get('total')} "
            f"elapsed={round(time.time() - t0, 2)}s"
        )
        return result

    # ── private: query handlers ──────────────────────────────

    def _list_customer_orders(self, body: dict, token: str) -> dict:
        """
        Fetch a paginated list of orders for a customer.

        body keys:
            customer_gid  str   — "gid://shopify/Customer/12345"
            page          int   — 1-based page number (default 1)
            per_page      int   — items per page (default 5)
            date_after    str   — ISO-8601 lower bound (optional)
            date_before   str   — ISO-8601 upper bound (optional)
            include       list  — specific order IDs to filter to (optional)
        """
        customer_gid = body.get("customer_gid")
        if not customer_gid:
            logger.warning("[ShopifyOrders] list_customer_orders: no customer_gid")
            return self._empty_result(body)

        page     = int(body.get("page", 1))
        per_page = int(body.get("per_page", 5))
        # We need to fetch enough orders to reach the requested page.
        # Shopify cursor pagination is forward-only, so we fetch page * per_page
        # items then slice. For typical use (page 1-3, per_page 1-20) this is
        # at most 60 items — well within a single request.
        fetch_count = page * per_page
        fetch_count = min(fetch_count, 250)  # Shopify max per request

        data = _gql(
            _CUSTOMER_ORDERS_GQL,
            {
                "customer_gid": customer_gid,
                "first":        fetch_count,
                "after":        None,
            },
            token,
        )

        customer_data = data.get("customer") or {}
        orders_conn   = customer_data.get("orders") or {}
        edges         = orders_conn.get("edges") or []
        page_info     = orders_conn.get("pageInfo") or {}

        all_orders = [_normalise_order(edge["node"]) for edge in edges]

        # Optional: filter to specific order IDs (used by HISTORICAL_SEARCH)
        include_ids = body.get("include")
        if include_ids:
            include_set = {str(i) for i in include_ids}
            all_orders = [
                o for o in all_orders
                if (str(o.get("id", "")).split("/")[-1] in include_set
                    or str(o.get("number", "")) in include_set)
            ]

        # Optional: date filters (applied post-fetch as belt-and-braces)
        date_after  = body.get("date_after")
        date_before = body.get("date_before")
        if date_after or date_before:
            all_orders = _filter_by_date(all_orders, date_after, date_before)

        total = len(all_orders)
        pages = max(1, -(-total // per_page)) if total else 0

        # Slice to the requested page
        start  = (page - 1) * per_page
        sliced = all_orders[start: start + per_page]

        # If Shopify returned hasNextPage=True there may be more beyond what
        # we fetched, so be conservative about total.
        if page_info.get("hasNextPage") and len(edges) == fetch_count:
            # We don't know the true total; report at least one more page.
            total = max(total, fetch_count + 1)
            pages = max(pages, page + 1)

        return {
            "orders":   sliced,
            "total":    total,
            "pages":    pages,
            "page":     page,
            "per_page": per_page,
        }

    def _fetch_single_order(self, body: dict, token: str) -> dict:
        """
        Fetch a single order by GID or numeric ID.

        body keys:
            order_id  str|int  — Shopify order GID or numeric ID
        """
        raw_id = body.get("order_id")
        if not raw_id:
            logger.warning("[ShopifyOrders] fetch_order: no order_id in body")
            return self._empty_result(body)

        order_gid = _to_order_gid(str(raw_id))

        data  = _gql(_FETCH_ORDER_GQL, {"order_gid": order_gid}, token)
        node  = data.get("order")

        if not node:
            logger.warning(f"[ShopifyOrders] fetch_order: order not found | gid={order_gid}")
            return self._empty_result(body)

        order = _normalise_order(node)
        return {
            "orders":   [order],
            "total":    1,
            "pages":    1,
            "page":     1,
            "per_page": 1,
        }

    # ── private: helpers ─────────────────────────────────────

    def _get_token(self) -> str:
        """
        Load the Shopify Admin access token from the DB.
        Mirrors the pattern used in ShopifyGraphQLExecutor._get_token().
        """
        token_row = ShopifyToken.query.get(SHOPIFY_STORE_DOMAIN)
        if not token_row or token_row.is_expired:
            raise RuntimeError(
                "Shopify Admin token missing or expired — "
                "check ShopifyTokenManager startup"
            )
        return token_row.access_token
    
    
    def _create_order(self, body: dict, token: str) -> dict:
        customer_id = body.get("customer_id", "")
        line_items  = body.get("line_items", [])

        customer_gid = (
            customer_id if str(customer_id).startswith("gid://")
            else f"gid://shopify/Customer/{customer_id}"
        )

        # ── Fetch customer's default address ──
        shipping_address = None
        try:
            addr_data = _gql(_CUSTOMER_ADDRESS_GQL, {"customer_gid": customer_gid}, token)
            default_addr = (
                (addr_data.get("customer") or {}).get("defaultAddress") or {}
            )
            if default_addr.get("address1") or default_addr.get("city"):
                shipping_address = {
                    "firstName": default_addr.get("firstName") or "",
                    "lastName":  default_addr.get("lastName")  or "",
                    "address1":  default_addr.get("address1")  or "",
                    "address2":  default_addr.get("address2")  or "",
                    "city":      default_addr.get("city")      or "",
                    "province":  default_addr.get("province")  or "",
                    "zip":       default_addr.get("zip")       or "",
                    "country":   default_addr.get("country")   or "",
                    "phone":     default_addr.get("phone")     or "",
                }
                logger.debug(f"[ShopifyOrders] _create_order: resolved shipping from customer default address")
            else:
                logger.warning(f"[ShopifyOrders] _create_order: customer has no default address")
        except Exception as exc:
            logger.warning(f"[ShopifyOrders] _create_order: failed to fetch customer address | {exc}")

        # ── Build line items ──
        line_item_inputs = []
        for item in line_items:
            vid = item.get("variation_id") or item.get("variant_gid")
            pid = item.get("product_id")
            qty = item.get("quantity", 1)
            if vid:
                variant_gid = vid if str(vid).startswith("gid://") else f"gid://shopify/ProductVariant/{vid}"
                line_item_inputs.append({"variantId": variant_gid, "quantity": qty})
            elif pid:
                product_gid = pid if str(pid).startswith("gid://") else f"gid://shopify/Product/{pid}"
                line_item_inputs.append({"variantId": product_gid, "quantity": qty})

        # ── Create draft order ──
        draft_input = {
            "customerId": customer_gid,
            "lineItems":  line_item_inputs,
        }
        if shipping_address:
            draft_input["shippingAddress"] = shipping_address

        mutation = """
        mutation CreateDraftOrder($input: DraftOrderInput!) {
          draftOrderCreate(input: $input) {
            draftOrder {
              id
              name
              status
              totalPriceSet { shopMoney { amount } }
              customer { id }
            }
            userErrors { field message }
          }
        }
        """

        data   = _gql(mutation, {"input": draft_input}, token)
        result = (data.get("draftOrderCreate") or {})
        errors = result.get("userErrors") or []
        if errors:
            raise ValueError(f"Shopify draftOrderCreate errors: {errors}")

        draft    = result.get("draftOrder") or {}
        draft_id = draft.get("id", "")

        # ── Complete draft → real order ──
        complete_mutation = """
        mutation CompleteDraftOrder($id: ID!) {
          draftOrderComplete(id: $id) {
            draftOrder {
              order { id name }
            }
            userErrors { field message }
          }
        }
        """
        complete_data   = _gql(complete_mutation, {"id": draft_id}, token)
        complete_result = (complete_data.get("draftOrderComplete") or {})
        complete_errors = complete_result.get("userErrors") or []

        if complete_errors:
            logger.warning(f"[ShopifyOrders] draftOrderComplete errors: {complete_errors}")
            return {
                "success": True,
                "data": {
                    "id":     draft_id,
                    "number": (draft.get("name") or "").lstrip("#"),
                    "status": "draft",
                }
            }

        real_order = (complete_result.get("draftOrder") or {}).get("order") or {}
        return {
            "success": True,
            "data": {
                "id":     real_order.get("id") or draft_id,
                "number": (real_order.get("name") or draft.get("name") or "").lstrip("#"),
                "status": "processing",
            }
        }

    @staticmethod
    def _empty_result(body: dict) -> dict:
        return {
            "orders":   [],
            "total":    0,
            "pages":    0,
            "page":     int(body.get("page", 1)),
            "per_page": int(body.get("per_page", 5)),
        }


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _to_order_gid(raw: str) -> str:
    """Convert a numeric ID string or existing GID to a Shopify Order GID."""
    if raw.startswith("gid://"):
        return raw
    return f"gid://shopify/Order/{raw}"


def _filter_by_date(orders: list, date_after: Optional[str],
                    date_before: Optional[str]) -> list:
    """
    Filter normalised order dicts by date_created.
    date_after / date_before are ISO-8601 strings (e.g. "2024-01-01T00:00:00Z").
    Comparison is simple string comparison — ISO format sorts lexicographically.
    """
    result = []
    for o in orders:
        dc = o.get("date_created") or ""
        if date_after  and dc < date_after:
            continue
        if date_before and dc > date_before:
            continue
        result.append(o)
    return result