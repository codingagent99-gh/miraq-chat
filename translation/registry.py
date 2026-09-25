"""
translation/registry.py — provider name -> shared instance.

To add a backend: write a TranslationProvider subclass in
translation/providers/, add a factory line to _FACTORIES. A tenant switches
to it by setting features.translation.provider to that name (PUT
/admin/translation/tenants/<license_id>) — no restart, no code change in
the chat pipeline.

Provider URLs/keys are deployment config (app_config / .env): every tenant
using "indictrans2" shares the one IndicTrans2 service. Tenants only choose
WHICH provider and WHICH languages.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional

import app_config as cfg
from translation.base import TranslationProvider
from translation.providers import IndicTrans2Provider, LibreTranslateProvider


def _http_kw() -> dict:
    return dict(
        connect_timeout=cfg.TRANSLATION_CONNECT_TIMEOUT,
        read_timeout=cfg.TRANSLATION_READ_TIMEOUT,
        breaker_cooldown=cfg.TRANSLATION_BREAKER_COOLDOWN,
    )


_FACTORIES: Dict[str, Callable[[], TranslationProvider]] = {
    "libretranslate": lambda: LibreTranslateProvider(
        cfg.LIBRETRANSLATE_URL, api_key=cfg.LIBRETRANSLATE_API_KEY, **_http_kw()
    ),
    "indictrans2": lambda: IndicTrans2Provider(cfg.INDICTRANS2_URL, **_http_kw()),
}

_instances: Dict[str, TranslationProvider] = {}
_lock = threading.Lock()


def provider_names() -> list:
    return sorted(_FACTORIES)


def get_provider(name: str) -> Optional[TranslationProvider]:
    name = (name or "").strip().lower()
    factory = _FACTORIES.get(name)
    if factory is None:
        return None
    with _lock:
        inst = _instances.get(name)
        if inst is None:
            inst = factory()
            _instances[name] = inst
        return inst
