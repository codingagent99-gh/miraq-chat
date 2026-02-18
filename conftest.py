"""
Pytest configuration for miraq-chat integration tests.

Instead of mocking StoreLoader, we initialize the REAL StoreLoader
that fetches live data from WooCommerce, exactly as server.py does at startup.
"""

import pytest
from store_loader import StoreLoader
from store_registry import set_store_loader, get_store_loader


# ─── Server URL (the real hosted server) ───
SERVER_URL = "http://localhost:5009"


@pytest.fixture(scope="session", autouse=True)
def live_store_loader():
    """
    Session-scoped fixture that initializes the real StoreLoader
    with live WooCommerce data and registers it in store_registry.

    This is the same initialization that server.py does at startup,
    ensuring classifier tests use real product/category/tag data.
    """
    loader = StoreLoader()
    try:
        loader.load_all()
    except Exception as e:
        pytest.skip(f"Could not load store data from WooCommerce: {e}")
    set_store_loader(loader)
    yield loader
    set_store_loader(None)


@pytest.fixture(scope="session")
def server_url():
    """Return the base URL of the hosted chat server."""
    return SERVER_URL


@pytest.fixture(scope="function")
def store_loader():
    """
    Function-scoped fixture that returns the current StoreLoader.
    Useful for tests that need direct access to the loader.
    """
    return get_store_loader()
