"""
shopify_apps.py — which Shopify app's credentials to use for a store.

The backend serves two kinds of Shopify install:

  * the PUBLIC app — one client id/secret from .env (SHOPIFY_CLIENT_ID /
    SHOPIFY_CLIENT_SECRET), shared by every store that installs it;
  * CUSTOM-distribution apps — one row each in shopify_apps
    (models/shopify_app.py), each with its own client id/secret.

Everything that used the global pair now asks this module instead:

    app_for_shop(shop)     App Proxy signatures, webhook HMACs, token refresh,
                           customer sign-in, the post-install page. Returns the
                           custom app the store INSTALLED through, else the
                           public app.
    app_for_install(id)    the install + OAuth callback routes, which know the
                           app from their URL (/shopify/install/<client_id>)
                           because the store has not installed anything yet.
    record_install(...)    called by the OAuth callback once Shopify has
                           verified the install; binds the store to that app.

`shop` is usually UNVERIFIED when looked up (a query param or a header). That
is fine: it only chooses WHICH secret the request's signature is checked
against. A forged shop just picks a secret the forger doesn't have.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple

from chat_logger import get_logger

logger = get_logger("miraq_chat")


@dataclass(frozen=True)
class AppCredentials:
    client_id: str
    client_secret: str = field(repr=False)
    custom: bool = False
    shops: Tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def normalise_shop(shop) -> str:
    return str(shop or "").strip().lower()


def public_app() -> AppCredentials:
    from app_config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
    return AppCredentials(SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, custom=False)


def _from_row(row) -> Optional[AppCredentials]:
    from tenant_crypto import decrypt_secret
    try:
        secret = decrypt_secret(row.client_secret_encrypted)
    except Exception as e:
        logger.error(f"shopify_apps: cannot decrypt secret for client_id={row.client_id!r} | {e}")
        return None
    return AppCredentials(
        client_id=row.client_id,
        client_secret=secret,
        custom=True,
        shops=tuple(row.shops or ()),
    )


def app_for_install(client_id: Optional[str]) -> Optional[AppCredentials]:
    """The app named in an install/callback URL. None client_id = the public app.
    Returns None for an unknown custom client id."""
    if not client_id:
        return public_app()
    from models.shopify_app import ShopifyApp
    row = ShopifyApp.query.get(client_id)
    return _from_row(row) if row is not None else None


def app_for_shop(shop) -> AppCredentials:
    """The app this store installed through; the public app if none (or on error)."""
    shop = normalise_shop(shop)
    if not shop:
        return public_app()
    try:
        from models.shopify_app import ShopifyApp
        row = ShopifyApp.query.filter(ShopifyApp.installed_shops.contains([shop])).first()
    except Exception as e:
        logger.error(f"shopify_apps: lookup failed for shop={shop!r}, using the public app | {e}")
        return public_app()
    if row is None:
        return public_app()
    return _from_row(row) or public_app()


def record_install(shop: str, app: AppCredentials) -> None:
    """
    Bind `shop` to the app it just installed through (verified OAuth callback).

    A store has exactly one active app: installing through app B removes it
    from app A's installed_shops. A public-app install removes it from every
    custom app, so the store goes back to the .env credentials.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from models import db
    from models.shopify_app import ShopifyApp

    shop = normalise_shop(shop)
    changed = False
    for row in ShopifyApp.query.filter(ShopifyApp.installed_shops.contains([shop])).all():
        if app.custom and row.client_id == app.client_id:
            continue
        row.installed_shops = [s for s in (row.installed_shops or []) if s != shop]
        flag_modified(row, "installed_shops")
        changed = True
        logger.info(f"shopify_apps: {shop} no longer on custom app {row.client_id}")

    if app.custom:
        row = ShopifyApp.query.get(app.client_id)
        if row is not None and shop not in (row.installed_shops or []):
            row.installed_shops = list(row.installed_shops or []) + [shop]
            flag_modified(row, "installed_shops")
            changed = True
            logger.info(f"shopify_apps: {shop} installed through custom app {app.client_id}")

    if changed:
        db.session.commit()


def install_path(app: AppCredentials) -> str:
    return f"shopify/install/{app.client_id}" if app.custom else "shopify/install"


def callback_path(app: AppCredentials) -> str:
    return f"shopify/auth/callback/{app.client_id}" if app.custom else "shopify/auth/callback"
