"""
catalog_events.py — Apply Shopify product webhooks to the in-memory catalog.

products/create, products/update and products/delete used to be verified,
logged and dropped, so a product edited in the Shopify admin (an option
renamed Colors -> Color, a new variant, a price) only reached the bot on the
6-hourly full refresh. A PM2 restart did not help either: the registry
rehydrates from the on-disk snapshot, which was just as old.

Flow
  1. The webhook route verifies the delivery and calls enqueue_product_event().
     It returns 200 straight away — Shopify gives a delivery 5 seconds.
  2. A single background thread per process waits until a store has been
     quiet for _QUIET_SECONDS (or _MAX_WAIT_SECONDS since its first pending
     event), so a bulk edit of 300 products is applied as ONE batch.
  3. Each changed product is fetched fresh by id with the same GraphQL query
     and normaliser the full load uses (the webhook body is REST-shaped and
     may be trimmed or arrive out of order, so it is never trusted as data).
     Deleted, draft and archived products are removed.
  4. Attributes and tags are re-aggregated from the products in memory, the
     lookup indexes are rebuilt and swapped in atomically (build_all_lookups),
     and the snapshot on disk is rewritten.
  5. Other gunicorn workers have their own copy of the catalog. They pick the
     new snapshot up through TenantRegistry.sync_from_snapshot_if_newer(),
     which compares the snapshot file's mtime on their next request.

Collections are not covered: products/update does not report collection
membership changes, so those still follow the 6-hourly refresh.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_QUIET_SECONDS = 4
_MAX_WAIT_SECONDS = 30
_SEEN_IDS_MAX = 2000

_lock = threading.Lock()
# tenant_id -> {"app": app, "first": ts, "last": ts, "changes": {numeric_id: (topic, gid)}}
_pending: Dict[str, dict] = {}
_seen_webhook_ids: "dict[str, float]" = {}
_worker: Optional[threading.Thread] = None


def _numeric_id(payload: dict) -> Optional[int]:
    raw = payload.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        gid = str(payload.get("admin_graphql_api_id") or "")
        tail = gid.rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None


def enqueue_product_event(app, tenant_id: str, topic: str, payload: dict,
                          webhook_id: str = "") -> bool:
    """Queue one verified product webhook. Returns False if it was ignored
    (duplicate delivery, unknown topic or no product id)."""
    topic = (topic or "").strip().lower()
    if topic not in ("products/create", "products/update", "products/delete"):
        logger.info(f"catalog_events: ignored topic {topic!r} | tenant={tenant_id}")
        return False

    pid = _numeric_id(payload or {})
    if pid is None:
        logger.warning(f"catalog_events: no product id in {topic} payload | tenant={tenant_id}")
        return False
    gid = (payload or {}).get("admin_graphql_api_id") or f"gid://shopify/Product/{pid}"

    now = time.time()
    with _lock:
        if webhook_id:
            if webhook_id in _seen_webhook_ids:
                logger.info(f"catalog_events: duplicate delivery {webhook_id!r} ignored")
                return False
            _seen_webhook_ids[webhook_id] = now
            if len(_seen_webhook_ids) > _SEEN_IDS_MAX:
                for k, _ in sorted(_seen_webhook_ids.items(), key=lambda kv: kv[1])[: _SEEN_IDS_MAX // 2]:
                    _seen_webhook_ids.pop(k, None)

        entry = _pending.setdefault(tenant_id, {"app": app, "first": now, "last": now, "changes": {}})
        entry["last"] = now
        entry["app"] = app
        # Last event per product wins: update-then-delete is a delete.
        entry["changes"][pid] = (topic, gid)
        _ensure_worker()

    logger.info(
        f"catalog_events: queued {topic} | product={pid} | tenant={tenant_id} | "
        f"pending={len(_pending[tenant_id]['changes'])}"
    )
    return True


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="catalog-events", daemon=True)
        _worker.start()


def _run():
    while True:
        time.sleep(1)
        due = []
        now = time.time()
        with _lock:
            for tid, entry in list(_pending.items()):
                if now - entry["last"] >= _QUIET_SECONDS or now - entry["first"] >= _MAX_WAIT_SECONDS:
                    due.append((tid, _pending.pop(tid)))
        for tid, entry in due:
            try:
                _apply_batch(entry["app"], tid, entry["changes"])
            except Exception as e:
                logger.error(f"catalog_events: batch failed | tenant={tid} | {e}", exc_info=True)


def _apply_batch(app, tenant_id: str, changes: dict) -> None:
    from models import Tenant
    from store_registry import get_tenant_registry
    from store_loader.lookup_builder import build_all_lookups
    from store_loader.shopify_fetcher import (
        fetch_single_product, _aggregate_attributes, _aggregate_tags,
    )
    from tenant_snapshot_store import snapshot_store, loader_to_snapshot_dict

    with app.app_context():
        registry = get_tenant_registry()
        if registry is None:
            return
        loader = dict(registry.resident_loaders()).get(tenant_id)
        if loader is None:
            # Not in memory in this worker. Bring it in (from the snapshot,
            # cheap) so the snapshot on disk gets the change and every other
            # worker follows it.
            if not snapshot_store.exists(tenant_id):
                logger.info(f"catalog_events: no catalog loaded or saved yet | tenant={tenant_id} — next load is live anyway")
                return
            row = Tenant.query.filter(Tenant.tenant_id == tenant_id).first()
            if row is None:
                return
            loader = registry.get_loader(row)

        if getattr(loader, "ecommerce_backend", "") != "shopify":
            return

        # Fetch outside the loader lock: network time must not block a full
        # refresh, and a full refresh running now already sees these changes.
        fetched: Dict[int, Optional[dict]] = {}
        removed = set()
        for pid, (topic, gid) in changes.items():
            if topic == "products/delete":
                removed.add(pid)
                continue
            product = fetch_single_product(
                loader.shopify_domain, loader._get_shopify_token(), gid,
                token_manager=loader._token_manager,
            )
            if product is None:
                # Network error or product gone. Keep what we have; the
                # 6-hourly refresh is the backstop.
                logger.warning(f"catalog_events: could not fetch product {pid} | tenant={tenant_id} — left unchanged")
                continue
            if (product.get("status") or "active") != "active":
                removed.add(pid)
            else:
                fetched[pid] = product

        if not fetched and not removed:
            return

        if not loader._lock.acquire(timeout=120):
            logger.warning(f"catalog_events: loader busy for 120s | tenant={tenant_id} — batch dropped, refresh will cover it")
            return
        try:
            products = []
            replaced = set()
            dropped = 0
            for p in loader.products:
                pid = p.get("id")
                if pid in removed:
                    dropped += 1
                    continue
                if pid in fetched:
                    products.append(fetched[pid])
                    replaced.add(pid)
                else:
                    products.append(p)
            added = [p for pid, p in fetched.items() if pid not in replaced]
            products.extend(added)

            build_all_lookups(loader, raw={
                "categories":              loader.categories,
                "tags":                    _aggregate_tags(products),
                "products":                products,
                "all_attributes_raw":      _aggregate_attributes(products),
                "currency_symbol":         loader.currency_symbol,
                "_expected_product_count": len(products),
            })
            loader._validate_load()
            if loader._degraded:
                logger.warning(
                    f"catalog_events: catalog degraded after update | tenant={tenant_id} | "
                    f"reasons={loader._degraded_reasons} — snapshot not saved"
                )
                return
            snapshot_store.save(tenant_id, loader_to_snapshot_dict(loader))
            loader._snapshot_mtime = snapshot_store.mtime(tenant_id)
        finally:
            loader._lock.release()

        logger.info(
            f"catalog_events: ✅ applied | tenant={tenant_id} | updated={len(replaced)} "
            f"added={len(added)} removed={dropped} "
            f"| products now={len(loader.products)}"
        )