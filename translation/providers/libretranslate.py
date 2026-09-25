"""
translation/providers/libretranslate.py — self-hosted LibreTranslate.

Runs under PM2 (libretranslate.config.js). Only the languages passed to
--load-only are available; /languages reports exactly those, so the admin
endpoint rejects a tenant language this instance cannot serve.
"""

from __future__ import annotations

import threading
import time
from typing import List, Sequence

from translation.base import (
    Detection, HttpProvider, TranslationUnavailable, as_text_list, normalise_confidence,
)

_LANG_CACHE_TTL = 600  # seconds


class LibreTranslateProvider(HttpProvider):
    name = "libretranslate"

    def __init__(self, base_url: str, api_key: str = "", **kw):
        super().__init__(base_url, **kw)
        self._api_key = api_key
        self._langs: set = set()
        self._langs_at = 0.0
        self._langs_lock = threading.Lock()

    def _body(self, **fields) -> dict:
        if self._api_key:
            fields["api_key"] = self._api_key
        return fields

    def supported_languages(self) -> set:
        with self._langs_lock:
            if self._langs and time.monotonic() - self._langs_at < _LANG_CACHE_TTL:
                return set(self._langs)
        try:
            data = self._request("GET", "/languages", timeout=(1.0, 5))
        except TranslationUnavailable:
            return set(self._langs)  # last known (may be empty)
        langs = {str(l.get("code")) for l in data or [] if l.get("code")} - {"en"}
        with self._langs_lock:
            self._langs, self._langs_at = langs, time.monotonic()
        return set(langs)

    def detect(self, text: str) -> List[Detection]:
        try:
            data = self._request("POST", "/detect", json=self._body(q=text))
        except TranslationUnavailable:
            return []
        out = []
        for row in data or []:
            lang = row.get("language")
            if lang:
                out.append(Detection(str(lang), normalise_confidence(row.get("confidence"))))
        return out

    def translate_batch(self, texts: Sequence[str], source: str, target: str) -> List[str]:
        if not texts:
            return []
        data = self._request(
            "POST", "/translate",
            json=self._body(q=list(texts), source=source, target=target, format="text"),
        )
        result = as_text_list((data or {}).get("translatedText"), len(texts))
        if result is None:
            raise TranslationUnavailable("libretranslate: unexpected translatedText shape")
        return result
