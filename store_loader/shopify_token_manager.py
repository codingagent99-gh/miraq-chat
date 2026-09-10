"""
store_loader/shopify_token_manager.py — Shopify OAuth token lifecycle.

Responsibilities:
  1. On startup: read saved token from Postgres.
     - If missing or expired → fetch a fresh one and save it.
     - If valid but near expiry (< 1 h) → use it now, trigger background refresh.
     - If healthy → use it directly.
  2. Background thread: checks every 30 minutes, refreshes when needed.
  3. get_token() → always returns a ready-to-use token (blocks briefly if a
     refresh is in progress).

Usage (called from store_loader/__init__.py):

    from store_loader.shopify_token_manager import ShopifyTokenManager
    token_mgr = ShopifyTokenManager(app=flask_app)
    token_mgr.start()
    token = token_mgr.get_token()   # use in every API call
"""

import threading
import time
import zlib
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from chat_logger import get_logger
from store_loader.config import (
    SHOPIFY_STORE_DOMAIN,
    SHOPIFY_CLIENT_ID,
    SHOPIFY_CLIENT_SECRET,
)

logger = get_logger("miraq_chat")

# How often the background thread wakes up to check token health (seconds)
_CHECK_INTERVAL = 30 * 60   # 30 minutes
# Retry interval after a failed refresh attempt
_RETRY_INTERVAL = 2  * 60   # 2 minutes


class ShopifyTokenManager:
    """
    Manages fetching, persisting, and background-refreshing the Shopify
    Admin API access token using the client_credentials OAuth flow.
    """

    def __init__(self, app=None):
        """
        Args:
            app: Flask app instance. If provided, all DB operations run inside
                 an app context (required when called from outside a request).
        """
        self._app    = app
        self._domain = SHOPIFY_STORE_DOMAIN

        self._lock           = threading.Lock()
        self._current_token: Optional[str] = None
        self._refresh_thread: Optional[threading.Thread] = None

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def start(self):
        """
        Load (or fetch) the initial token then start the background refresh loop.
        Call this once at server startup, after db.init_app() and db.create_all().

        A failed INITIAL fetch is deliberately not fatal. _do_refresh() logs and
        re-raises so that get_token() still surfaces the failure to whoever
        actually needs a token -- but letting that escape from here killed the
        whole process on a transient network blip at boot, while load_all() hit
        the very same failure, caught it, and carried on degraded. Two paths,
        two answers, and the strict one won by accident of ordering.

        Boot degraded instead: log loudly, start the retry loop anyway, and let
        the first request that genuinely needs a token be the thing that fails.
        """
        try:
            self._ensure_valid_token()
        except Exception as e:
            logger.error(
                "ShopifyTokenManager: ⚠️  startup token fetch failed — starting "
                "DEGRADED. Background loop will retry every "
                f"{_RETRY_INTERVAL // 60} min; requests needing a token will "
                f"fail until one succeeds. {type(e).__name__}: {e}",
                exc_info=True,
            )
        self._start_background_loop()

    def get_token(self) -> str:
        """
        Return the current access token.
        Blocks for at most a few seconds if a refresh is in progress.

        Raises RuntimeError if no valid token is available.
        """
        with self._lock:
            if self._current_token:
                return self._current_token

        # Token missing in memory — try to load from DB or fetch fresh
        self._ensure_valid_token()

        with self._lock:
            if self._current_token:
                return self._current_token

        raise RuntimeError(
            "ShopifyTokenManager: could not obtain a valid access token. "
            "Check SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET and server logs."
        )

    def invalidate(self, bad_token: str):
        """
        Called by the fetcher immediately after Shopify itself rejects
        `bad_token` with a 401 — i.e. Shopify has already told us this
        token is dead, regardless of what our own expiry bookkeeping says.

        Drops it from memory and forces a synchronous refresh, so the next
        get_token() call returns something usable right away instead of
        the same dead token being handed out again until the next
        scheduled health check (up to 30 minutes later).

        If `bad_token` no longer matches what's cached — another thread
        already replaced it, via its own invalidate() call or the routine
        health check — this is a no-op.
        """
        with self._lock:
            if self._current_token != bad_token:
                return
            self._current_token = None
        self._do_refresh(known_bad_token=bad_token)

    # ──────────────────────────────────────────────
    # Internal: token lifecycle
    # ──────────────────────────────────────────────

    def _ensure_valid_token(self):
        """
        Check DB for a saved token.
        - Valid & healthy → cache in memory, done.
        - Near-expiry     → cache current token (still usable), kick off refresh.
        - Expired/missing → block and fetch a new one now.
        """
        row = self._load_from_db()

        if row and not row.is_expired:
            with self._lock:
                self._current_token = row.access_token

            if row.needs_refresh:
                logger.info(
                    f"ShopifyTokenManager: token expires in "
                    f"{row.seconds_until_expiry / 3600:.1f}h — scheduling proactive refresh"
                )
                threading.Thread(target=self._do_refresh, daemon=True).start()
            else:
                logger.info(
                    f"ShopifyTokenManager: ✅ loaded token from DB "
                    f"(expires in {row.seconds_until_expiry / 3600:.1f}h, "
                    f"refreshed {row.refresh_count}× so far)"
                )
        else:
            reason = "not found in DB" if not row else "expired"
            logger.info(f"ShopifyTokenManager: token {reason} — fetching fresh token…")
            self._do_refresh()

    def _do_refresh(self, known_bad_token: Optional[str] = None):
        """
        Hit the Shopify client_credentials endpoint, persist the result to
        Postgres, and update the in-memory cache.

        Guarded by a Postgres transaction-scoped advisory lock keyed on the
        store domain. Shopify's client_credentials grant invalidates the
        previously issued token every time a new one is requested for the
        same client -- so with multiple gunicorn workers each running their
        own ShopifyTokenManager, two workers refreshing at the same moment
        used to silently knock each other's token offline (the loser kept a
        token that Shopify had already killed, and had no way to notice).
        The lock serialises refreshes across workers; the re-check
        immediately after acquiring it lets a worker that lost the race
        just adopt the token the winner already wrote, instead of fetching
        -- and thereby invalidating -- a second one.

        `known_bad_token`, set by invalidate(), means this call was
        triggered by an actual 401 rather than routine expiry bookkeeping.
        In that case a DB row is only trusted if it holds a *different*
        token than the one that just got rejected -- our own timestamps
        saying a row is "healthy" don't mean much when Shopify has already
        told us, empirically, that its token doesn't work.

        The lock is held for the lifetime of this transaction, which spans
        the outbound HTTP call to Shopify. That's deliberate: an
        xact-scoped lock can't be released early and can't leak on a pooled
        connection, and refreshes are rare (roughly once per token
        lifetime), so tying up one connection for the duration is cheap.
        """
        from models import db
        from models.shopify_token import ShopifyToken
        from sqlalchemy import text

        lock_key = zlib.crc32(self._domain.encode("utf-8"))

        try:
            with self._db_context():
                db.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

                # Re-check now that we hold the lock — another worker may
                # have refreshed while we were waiting for it.
                row = ShopifyToken.query.get(self._domain)
                row_is_usable = (
                    row and not row.needs_refresh
                    and (known_bad_token is None or row.access_token != known_bad_token)
                )
                if row_is_usable:
                    with self._lock:
                        self._current_token = row.access_token
                    logger.info(
                        "ShopifyTokenManager: ✅ another worker already refreshed "
                        "the token while we waited for the lock — adopting it "
                        f"(expires in {row.seconds_until_expiry / 3600:.1f}h)"
                    )
                    db.session.commit()  # release the advisory lock
                    return

                url = f"https://{self._domain}/admin/oauth/access_token"
                params = {
                    "grant_type":    "client_credentials",
                    "client_id":     SHOPIFY_CLIENT_ID,
                    "client_secret": SHOPIFY_CLIENT_SECRET,
                }
                resp = requests.post(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                access_token = data["access_token"]
                scope        = data.get("scope", "")
                expires_in   = int(data.get("expires_in", 86400))   # default 24 h

                now        = datetime.now(timezone.utc)
                expires_at = now + timedelta(seconds=expires_in)

                if row:
                    row.access_token  = access_token
                    row.scope         = scope
                    row.fetched_at    = now
                    row.expires_at    = expires_at
                    row.refresh_count = (row.refresh_count or 0) + 1
                    row.last_error    = None
                else:
                    row = ShopifyToken(
                        store_domain  = self._domain,
                        access_token  = access_token,
                        scope         = scope,
                        fetched_at    = now,
                        expires_at    = expires_at,
                        refresh_count = 1,
                    )
                    db.session.add(row)
                db.session.commit()  # persists the token and releases the lock

                with self._lock:
                    self._current_token = access_token

                logger.info(
                    f"ShopifyTokenManager: ✅ token refreshed — "
                    f"expires at {expires_at.isoformat()} "
                    f"(in {expires_in / 3600:.1f}h)"
                )

        except Exception as e:
            logger.error(
                f"ShopifyTokenManager: ❌ token refresh failed — {type(e).__name__}: {e}",
                exc_info=True,
            )
            # Persist the error so it shows up in the shopify_tokens row
            self._save_error_to_db(str(e))
            raise

    # ──────────────────────────────────────────────
    # Internal: DB helpers
    # ──────────────────────────────────────────────

    def _db_context(self):
        """
        Returns a context manager that wraps DB operations in a Flask app context
        if we have one, otherwise is a no-op (we're already inside a request context).
        """
        if self._app:
            return self._app.app_context()
        # Already inside an app/request context — no-op context manager
        from contextlib import nullcontext
        return nullcontext()

    def _load_from_db(self):
        """Load the ShopifyToken row for this domain, or None."""
        from models.shopify_token import ShopifyToken
        try:
            with self._db_context():
                return ShopifyToken.query.get(self._domain)
        except Exception as e:
            logger.error(f"ShopifyTokenManager: DB read failed — {e}", exc_info=True)
            return None

    def _save_error_to_db(self, error_msg: str):
        """Record the last refresh error in the token row (if one exists)."""
        from models.shopify_token import ShopifyToken
        from models import db
        try:
            with self._db_context():
                row = ShopifyToken.query.get(self._domain)
                if row:
                    row.last_error = error_msg
                    db.session.commit()
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # Internal: background loop
    # ──────────────────────────────────────────────

    def _start_background_loop(self):
        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def _loop():
            while True:
                # Retry on the SHORT interval while we hold no usable token at
                # all (i.e. the startup fetch failed and we booted degraded),
                # and on the normal health-check cadence once one is in hand.
                # Sleeping the full check interval first would leave a degraded
                # boot unrecoverable for 30 minutes even after the network came
                # back.
                with self._lock:
                    _have_token = bool(self._current_token)
                time.sleep(_CHECK_INTERVAL if _have_token else _RETRY_INTERVAL)
                try:
                    row = self._load_from_db()
                    if not row or row.needs_refresh:
                        label = "expired/missing" if (not row or row.is_expired) else "near-expiry"
                        logger.info(f"ShopifyTokenManager: 🔄 background refresh triggered ({label})")
                        self._do_refresh()
                    else:
                        # The DB row looks healthy, but our in-memory copy may
                        # not match it -- e.g. another worker refreshed the
                        # token (which invalidates whatever we're holding)
                        # since our last check. Re-sync unconditionally so a
                        # worker that lost that race recovers here instead of
                        # failing every request until it's restarted.
                        with self._lock:
                            was_stale = self._current_token != row.access_token
                            self._current_token = row.access_token
                        if was_stale:
                            logger.info(
                                "ShopifyTokenManager: 🔁 in-memory token was stale "
                                "(refreshed by another worker) — synced from DB "
                                f"({row.seconds_until_expiry / 3600:.1f}h remaining)"
                            )
                        else:
                            logger.debug(
                                f"ShopifyTokenManager: token healthy "
                                f"({row.seconds_until_expiry / 3600:.1f}h remaining)"
                            )
                except Exception as e:
                    logger.error(
                        f"ShopifyTokenManager: background loop error — {e}", exc_info=True
                    )
                    time.sleep(_RETRY_INTERVAL)

        self._refresh_thread = threading.Thread(target=_loop, daemon=True, name="shopify-token-refresh")
        self._refresh_thread.start()
        logger.info("ShopifyTokenManager: background refresh loop started (checks every 30 min)")