"""
translation/base.py — the contract every translation backend implements.

The chat pipeline only ever talks to TranslationProvider. LibreTranslate and
IndicTrans2 are two implementations; adding a third (Bhashini, Sarvam, a
cloud API) means one new class in translation/providers/ and one entry in
translation/registry.py — nothing in routes/chat.py changes.

LANGUAGE CODES
    Everything above the provider speaks ISO 639-1 ("en", "mr", "hi") — the
    same codes stored in tenants.features["translation"]. A provider that
    needs different codes (IndicTrans2 wants "mar_Deva") maps them itself.

FAILURE CONTRACT
    translate_batch() raises TranslationUnavailable on ANY failure. Callers
    catch it and fall back to the untranslated text — a translator outage must
    degrade to "English only", never to a failed chat turn.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Sequence

import requests

from chat_logger import get_logger

logger = get_logger("miraq_translation")


class TranslationUnavailable(Exception):
    """The provider could not produce a translation (down, timeout, bad reply)."""


@dataclass(frozen=True)
class Detection:
    language: str      # ISO 639-1
    confidence: float  # normalised to 0..1


class CircuitBreaker:
    """
    Stop dialling a provider that is not listening.

    Only connection failures trip it: a timeout or an HTTP error means
    something IS there and is worth retrying normally. One breaker per
    provider, so LibreTranslate being down never silences IndicTrans2.
    """

    def __init__(self, name: str, cooldown: float):
        self._name = name
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def trip(self, url: str) -> None:
        with self._lock:
            was_closed = time.monotonic() >= self._open_until
            self._open_until = time.monotonic() + self._cooldown
        if was_closed:  # log the transition only, not every request
            logger.error(
                "[Translate] %s not reachable at %s — skipping it for %.0fs",
                self._name, url, self._cooldown,
            )

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0


class TranslationProvider(ABC):
    """One translation backend. Instances are shared across threads."""

    name: str = ""

    @abstractmethod
    def supported_languages(self) -> set:
        """ISO codes this provider can translate to/from English (excluding "en")."""

    @abstractmethod
    def detect(self, text: str) -> List[Detection]:
        """Ranked guesses, best first. Empty list = cannot tell (not an error)."""

    @abstractmethod
    def translate_batch(self, texts: Sequence[str], source: str, target: str) -> List[str]:
        """Translate each item. Same length and order as `texts`. Raises TranslationUnavailable."""

    def health(self) -> dict:
        return {"provider": self.name, "reachable": None}


class HttpProvider(TranslationProvider):
    """Shared plumbing for providers reached over HTTP (both current ones are)."""

    def __init__(self, base_url: str, connect_timeout: float, read_timeout: float,
                 breaker_cooldown: float):
        self.base_url = base_url.rstrip("/")
        self._timeout = (connect_timeout, read_timeout)
        self._breaker = CircuitBreaker(self.name or self.__class__.__name__, breaker_cooldown)
        self._session = requests.Session()

    def _request(self, method: str, path: str, *, json=None, timeout=None):
        """Returns parsed JSON or raises TranslationUnavailable."""
        if self._breaker.is_open():
            raise TranslationUnavailable(f"{self.name}: circuit open")
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, json=json, timeout=timeout or self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ConnectionError as e:
            self._breaker.trip(url)
            raise TranslationUnavailable(f"{self.name}: connection failed") from e
        except requests.exceptions.Timeout as e:
            logger.error("[Translate] %s %s timed out after %ss", self.name, path, self._timeout)
            raise TranslationUnavailable(f"{self.name}: timeout") from e
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error("[Translate] %s %s failed: %s", self.name, path, e)
            raise TranslationUnavailable(f"{self.name}: {e}") from e
        self._breaker.reset()
        return data

    def health(self) -> dict:
        try:
            self._request("GET", "/languages", timeout=(1.0, 5))
            return {"provider": self.name, "url": self.base_url, "reachable": True}
        except TranslationUnavailable as e:
            return {"provider": self.name, "url": self.base_url, "reachable": False, "error": str(e)}


def normalise_confidence(value) -> float:
    """LibreTranslate reports 0..100 in current versions and 0..1 in old ones."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    return c / 100.0 if c > 1.0 else c


def as_text_list(value, expected: int) -> Optional[List[str]]:
    """Coerce a provider's translatedText into a list of `expected` strings, or None."""
    if isinstance(value, str) and expected == 1:
        value = [value]
    if not isinstance(value, list) or len(value) != expected:
        return None
    return [str(v or "") for v in value]
