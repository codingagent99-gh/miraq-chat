"""
store_registry.py — Singleton accessor for the live StoreLoader instance.

All hardcoded TAGS, ATTRIBUTES, COLOR_MAP etc. have been removed.
Everything now comes from the live StoreLoader fetched at startup.
"""

_store_loader = None


def set_store_loader(loader):
    global _store_loader
    _store_loader = loader


def get_store_loader():
    return _store_loader