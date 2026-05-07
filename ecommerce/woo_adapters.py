from __future__ import annotations

from copy import deepcopy
from typing import Iterable


def _pick_price(raw: dict) -> str:
    for key in ("sale_price", "price", "regular_price"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return "N/A"


def _original_price(raw: dict) -> str | None:
    regular = raw.get("regular_price")
    if regular in (None, ""):
        return None
    current = _pick_price(raw)
    regular = str(regular)
    return regular if current != regular else None


def _is_in_stock(raw: dict) -> bool:
    if isinstance(raw.get("in_stock"), bool):
        return raw["in_stock"]
    if raw.get("stock_status") is not None:
        return raw.get("stock_status") == "instock"
    qty = raw.get("inventory_quantity")
    if qty is not None:
        try:
            return int(qty) > 0
        except (TypeError, ValueError):
            return False
    return True


def normalize_address(raw: dict | None) -> dict:
    raw = raw or {}
    return {
        "first_name": raw.get("first_name", raw.get("firstName", "")) or "",
        "last_name": raw.get("last_name", raw.get("lastName", "")) or "",
        "address_1": raw.get("address_1", raw.get("address1", "")) or "",
        "address_2": raw.get("address_2", raw.get("address2", "")) or "",
        "city": raw.get("city", "") or "",
        "state": raw.get("state", "") or "",
        "postcode": raw.get("postcode", raw.get("zip", "")) or "",
        "country": raw.get("country", "") or "",
        "phone": raw.get("phone", "") or "",
    }


def _address_has_content(address: dict) -> bool:
    return any(address.get(key) for key in ("address_1", "city", "postcode", "phone"))


def _dedupe_addresses(addresses: Iterable[dict]) -> list[dict]:
    seen = set()
    result = []
    for address in addresses:
        if not _address_has_content(address):
            continue
        key = tuple(address.get(field, "") for field in (
            "first_name", "last_name", "address_1", "address_2", "city", "state", "postcode", "country", "phone",
        ))
        if key in seen:
            continue
        seen.add(key)
        result.append(address)
    return result


def _normalize_product_options(raw: dict) -> list[dict]:
    attributes = raw.get("attributes", [])
    if isinstance(attributes, dict):
        result = []
        for slug, attr_data in attributes.items():
            if isinstance(attr_data, dict):
                values = attr_data.get("options", [])
            elif isinstance(attr_data, list):
                values = attr_data
            else:
                values = [attr_data] if attr_data else []
            result.append({
                "name": slug.replace("pa_", "").replace("-", " ").title(),
                "values": [str(v) for v in values if str(v).strip()],
                "options": [str(v) for v in values if str(v).strip()],
                "is_variation": True,
            })
        return result

    result = []
    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        name = attr.get("name", "")
        values = [str(v) for v in attr.get("options", []) if str(v).strip()]
        result.append({
            "name": name.replace("pa_", "").replace("-", " ").title() if name.startswith("pa_") else name,
            "values": values,
            "options": values,
            "is_variation": attr.get("variation", False),
        })
    return result


def _normalize_variant_options(raw: dict) -> dict[str, str]:
    attrs = raw.get("attributes", [])
    if isinstance(attrs, dict):
        return {
            k.replace("attribute_", "").replace("pa_", "").replace("-", " ").title(): str(v).replace("-", " ").title()
            for k, v in attrs.items() if v not in (None, "")
        }

    result = {}
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        name = attr.get("name", "")
        option = attr.get("option", attr.get("value", ""))
        if not name or option in (None, ""):
            continue
        result[name.replace("pa_", "").replace("-", " ").title() if name.startswith("pa_") else name] = str(option)
    return result


def normalize_variant(raw: dict, *, parent: dict | None = None) -> dict:
    options = _normalize_variant_options(raw)
    label = " / ".join(str(value) for value in options.values() if value)
    parent_name = (parent or {}).get("name", "")
    image = raw.get("image", {})
    image_url = image.get("src", "") if isinstance(image, dict) else ""
    normalized = {
        "id": raw.get("id"),
        "parent_id": raw.get("parent_id") or (parent or {}).get("id"),
        "name": f"{parent_name} — {label}" if parent_name and label else (parent_name or raw.get("name", "")),
        "type": "variation",
        "price": _pick_price(raw),
        "original_price": _original_price(raw),
        "on_sale": bool(raw.get("on_sale") or _original_price(raw)),
        "in_stock": _is_in_stock(raw),
        "options": options,
        "attributes": [{"name": name, "value": value} for name, value in options.items()],
        "variation_label": label,
        "sku": raw.get("sku", ""),
        "image": image_url,
        "images": [image_url] if image_url else list((parent or {}).get("images", [])),
        "_raw": deepcopy(raw),
    }
    return normalized


def normalize_product(raw: dict) -> dict:
    images = []
    for image in raw.get("images", []):
        if isinstance(image, dict) and image.get("src"):
            images.append(image["src"])
        elif isinstance(image, str) and image:
            images.append(image)

    categories = []
    for category in raw.get("categories", []):
        if isinstance(category, dict):
            if category.get("name"):
                categories.append(category["name"])
        elif category:
            categories.append(str(category))

    tags = []
    for tag in raw.get("tags", []):
        if isinstance(tag, dict):
            if tag.get("name"):
                tags.append(tag["name"])
        elif tag:
            tags.append(str(tag))

    variant_ids = []
    raw_variations = raw.get("variations", [])
    normalized_variations = []
    if isinstance(raw_variations, list):
        for variation in raw_variations:
            if isinstance(variation, dict):
                normalized_variations.append(normalize_variant(variation, parent=raw))
                if variation.get("id") is not None:
                    variant_ids.append(variation.get("id"))
            elif variation is not None:
                variant_ids.append(variation)

    options = _normalize_product_options(raw)
    normalized = {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "type": raw.get("type", "variable" if variant_ids else "simple"),
        "price": _pick_price(raw),
        "original_price": _original_price(raw),
        "on_sale": bool(raw.get("on_sale") or _original_price(raw)),
        "in_stock": _is_in_stock(raw) or any(v.get("in_stock") for v in normalized_variations),
        "stock_quantity": raw.get("stock_quantity"),
        "options": options,
        "attributes": options,
        "variant_ids": variant_ids,
        "variations": normalized_variations,
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": raw.get("permalink", ""),
        "categories": categories,
        "tags": tags,
        "images": images,
        "description": raw.get("description", ""),
        "short_description": raw.get("short_description", ""),
        "average_rating": raw.get("average_rating", "0"),
        "rating_count": raw.get("rating_count", 0),
        "weight": raw.get("weight", ""),
        "dimensions": raw.get("dimensions", {}),
        "total_sales": raw.get("total_sales", 0),
        "_raw": deepcopy(raw),
    }
    return normalized


def normalize_line_item(raw: dict) -> dict:
    price = raw.get("price", 0) or 0
    total = raw.get("total", 0) or 0
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 0.0
    return {
        "name": raw.get("name", "Unknown Item"),
        "quantity": raw.get("quantity", 1),
        "price": price,
        "total": total,
        "sku": raw.get("sku", ""),
        "product_id": raw.get("product_id"),
        "variant_id": raw.get("variation_id", raw.get("variant_id")),
    }


def normalize_order(raw: dict) -> dict:
    normalized = {
        "id": raw.get("id"),
        "number": str(raw.get("number") or raw.get("id", "")),
        "status": raw.get("status", "unknown"),
        "currency_symbol": raw.get("currency_symbol", ""),
        "total": str(raw.get("total", "0")),
        "subtotal": str(raw.get("subtotal", raw.get("total", "0"))),
        "shipping_total": str(raw.get("shipping_total", "0")),
        "payment_method_label": raw.get("payment_method_title", ""),
        "created_at": raw.get("date_created", raw.get("created_at", "")) or "",
        "paid_at": raw.get("date_paid", raw.get("processed_at")),
        "line_items": [normalize_line_item(item) for item in raw.get("line_items", []) if isinstance(item, dict)],
        "billing_address": normalize_address(raw.get("billing")),
        "shipping_address": normalize_address(raw.get("shipping")),
        "customer_id": raw.get("customer_id"),
        "_raw": deepcopy(raw),
    }
    return normalized


def normalize_customer(raw: dict) -> dict:
    billing_address = normalize_address(raw.get("billing"))
    shipping_address = normalize_address(raw.get("shipping"))
    addresses = _dedupe_addresses([shipping_address, billing_address])
    default_address = shipping_address if _address_has_content(shipping_address) else billing_address
    return {
        "id": raw.get("id"),
        "first_name": raw.get("first_name", "") or "",
        "last_name": raw.get("last_name", "") or "",
        "email": raw.get("email", "") or "",
        "default_address": default_address,
        "addresses": addresses,
        "shipping_address": shipping_address,
        "billing_address": billing_address,
        "_raw": deepcopy(raw),
    }


def normalize_category(raw: dict) -> dict:
    image = raw.get("image")
    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "parent": raw.get("parent", 0),
        "count": raw.get("count", 0),
        "description": raw.get("description", ""),
        "image": image.get("src", "") if isinstance(image, dict) else "",
        "_raw": deepcopy(raw),
    }


def normalize_tag(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "count": raw.get("count", 0),
        "_raw": deepcopy(raw),
    }


def normalize_currency(raw: dict) -> dict:
    return {"code": raw.get("code", ""), "symbol": raw.get("symbol", ""), "_raw": deepcopy(raw)}


def _normalize_items(items: list, normalizer) -> list:
    return [normalizer(item) for item in items if isinstance(item, dict)]


def _normalize_order_payload(payload: dict) -> dict:
    line_items = []
    for item in payload.get("line_items", []):
        if not isinstance(item, dict):
            continue
        line_items.append({
            "product_id": item.get("product_id"),
            "quantity": item.get("quantity", 1),
            **({"variation_id": item.get("variant_id")} if item.get("variant_id") else {}),
        })

    body = {
        "status": payload.get("status", "processing"),
        "customer_id": payload.get("customer_id"),
        "payment_method": payload.get("payment_method", "cod"),
        "payment_method_title": payload.get("payment_method_label", payload.get("payment_method", "Cash on Delivery")),
        "set_paid": payload.get("set_paid", False),
        "line_items": line_items,
    }
    shipping = payload.get("shipping_address") or payload.get("default_address") or {}
    billing = payload.get("billing_address") or shipping
    if shipping:
        body["shipping"] = shipping
        body["billing"] = billing
    return body


def _normalize_customer_payload(payload: dict) -> dict:
    addresses = [a for a in payload.get("addresses", []) if isinstance(a, dict)]
    default_address = payload.get("default_address") or (addresses[0] if addresses else {})
    shipping = addresses[0] if addresses else default_address
    billing = addresses[1] if len(addresses) > 1 else default_address
    return {
        "first_name": payload.get("first_name", ""),
        "last_name": payload.get("last_name", ""),
        "email": payload.get("email", ""),
        "billing": billing,
        "shipping": shipping,
    }


def normalize_call_payload(operation: str, payload: dict | None) -> dict | None:
    if payload is None:
        return None
    if operation == "create_order":
        return _normalize_order_payload(payload)
    if operation == "update_customer":
        return _normalize_customer_payload(payload)
    return payload


def normalize_response(operation: str, data, headers: dict | None = None) -> tuple[object, str | None, str | None]:
    headers = headers or {}
    total = headers.get("X-WP-Total")
    total_pages = headers.get("X-WP-TotalPages")

    if operation in {"fetch_product", "product_detail"} and isinstance(data, dict):
        return normalize_product(data), total, total_pages
    if operation in {"fetch_variant"} and isinstance(data, dict):
        return normalize_variant(data), total, total_pages
    if operation in {"list_variants"} and isinstance(data, list):
        return _normalize_items(data, normalize_variant), total, total_pages
    if operation in {"products_advanced", "historical_product_search", "check_stock"} and isinstance(data, dict):
        products = _normalize_items(data.get("products", []), normalize_product)
        return {"products": products, "total": int(data.get("total", len(products)) or 0), "pages": int(data.get("pages", 1) or 1), "_raw": deepcopy(data)}, str(data.get("total", len(products)) or 0), str(data.get("pages", 1) or 1)
    if operation in {"fetch_order"} and isinstance(data, dict):
        return normalize_order(data), total, total_pages
    if operation in {"list_customer_orders", "list_customer_orders_custom"}:
        if isinstance(data, dict) and "orders" in data:
            orders = _normalize_items(data.get("orders", []), normalize_order)
            return orders, str(data.get("total", len(orders)) or 0), str(data.get("pages", 1) or 1)
        if isinstance(data, list):
            return _normalize_items(data, normalize_order), total, total_pages
    if operation == "create_order" and isinstance(data, dict):
        return normalize_order(data), total, total_pages
    if operation == "fetch_customer" and isinstance(data, dict):
        return normalize_customer(data), total, total_pages
    if operation == "update_customer" and isinstance(data, dict):
        return normalize_customer(data), total, total_pages
    if operation == "list_categories" and isinstance(data, list):
        return _normalize_items(data, normalize_category), total, total_pages
    if operation == "list_tags" and isinstance(data, list):
        return _normalize_items(data, normalize_tag), total, total_pages
    if operation == "list_products" and isinstance(data, list):
        return _normalize_items(data, normalize_product), total, total_pages
    if operation == "list_coupons" and isinstance(data, list):
        return {"items": deepcopy(data), "_raw": deepcopy(data)}, total, total_pages
    if operation == "fetch_wishlist":
        items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
        return {"items": deepcopy(items), "_raw": deepcopy(data)}, total, total_pages
    if operation == "fetch_currency" and isinstance(data, dict):
        return normalize_currency(data), total, total_pages
    return data, total, total_pages
