"""
language_utils.py
─────────────────
Detect language and translate to English using a locally hosted LibreTranslate instance.
"""

import threading
import time

import requests
from chat_logger import get_logger
import os

logger = get_logger("miraq_language")

# ── Config ───────────────────────────────────────────────────────────────────
PORT               = int(os.getenv("LIBRE_TRANSLATE_PORT", 5012))  # ← matches PM2 config
LIBRETRANSLATE_URL = f"http://localhost:{PORT}"
DETECT_ENDPOINT    = f"{LIBRETRANSLATE_URL}/detect"
TRANSLATE_ENDPOINT = f"{LIBRETRANSLATE_URL}/translate"

# Hard off-switch. Defaults OFF -- LibreTranslate isn't deployed/wanted
# right now, so skip the network entirely rather than paying a connect
# attempt (and, before the circuit breaker, ~6s) on every request. Set
# LANG_DETECTION_ENABLED=true once LibreTranslate is actually running.
ENABLED = os.getenv("LANG_DETECTION_ENABLED", "false").strip().lower() in ("true", "1", "yes")

MIN_CONFIDENCE         = 0.5
TARGET_LANG            = "en"
SUPPORTED_SOURCE_LANGS = {"es"}

# Detect is fast; translate can be slow on cold start (model lazy-loads).
#
# These are (connect, read) pairs, NOT single values. A bare timeout=15 also
# governs the CONNECT phase, so when LibreTranslate is not listening every
# request pays the full OS connect-failure cost before giving up. Measured on
# Windows (Sep 2026): ~6s per request -- more than half of an 11s request --
# because the connect is retried across both ::1 and 127.0.0.1. A short
# connect timeout bounds that; the read timeout stays generous for a service
# that is actually up but lazy-loading a model.
DETECT_TIMEOUT    = (1.0, 15)   # (connect, read) seconds
TRANSLATE_TIMEOUT = (1.0, 60)   # (connect, read) seconds

# ── Circuit breaker ──────────────────────────────────────────────────────────
# Without this, an unreachable LibreTranslate is re-dialled on EVERY request
# forever -- the cost is paid per request even though the answer ("still
# down") is known. After a connection failure we stop dialling for a cooldown
# and fall straight through to the English default. One probe per cooldown
# window, not one per request.
#
# Only ConnectionError trips this. A timeout or an HTTP error means something
# IS listening, which is a different failure worth retrying normally.
_BREAKER_COOLDOWN = float(os.getenv("LANG_BREAKER_COOLDOWN", "60"))  # seconds
_breaker_lock     = threading.Lock()
_breaker_open_until = 0.0


def _breaker_is_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_open_until


def _trip_breaker() -> None:
    global _breaker_open_until
    with _breaker_lock:
        was_closed = time.monotonic() >= _breaker_open_until
        _breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN
    # Log the transition only, so a down service does not spam one line per
    # request -- the silence afterwards is the breaker doing its job.
    if was_closed:
        logger.error(
            "[LangDetect] LibreTranslate not reachable at %s - skipping language "
            "detection for %.0fs (assuming English)",
            LIBRETRANSLATE_URL, _BREAKER_COOLDOWN,
        )


def _reset_breaker() -> None:
    global _breaker_open_until
    with _breaker_lock:
        _breaker_open_until = 0.0


# ── Core Functions ────────────────────────────────────────────────────────────

def detect_language(text: str) -> tuple[str, float]:
    # Every early return here is ("en", 0.0) -- the same value the exception
    # handlers below already return. Skipping the call is therefore not a
    # behaviour change, only a faster route to the same answer.
    if not ENABLED:
        return "en", 0.0

    if _breaker_is_open():
        return "en", 0.0

    try:
        response = requests.post(DETECT_ENDPOINT, json={"q": text}, timeout=DETECT_TIMEOUT)
        response.raise_for_status()
        results = response.json()

        if not results:
            logger.warning("[LangDetect] Empty results.")
            return "en", 0.0

        top        = results[0]
        lang       = top.get("language", "en")
        confidence = top.get("confidence", 0.0)

        logger.debug(f"[LangDetect] text='{text[:60]}' → lang={lang}, confidence={confidence:.2f}")
        _reset_breaker()
        return lang, confidence

    except requests.exceptions.ConnectionError:
        _trip_breaker()
        return "en", 0.0
    except requests.exceptions.Timeout:
        logger.error("[LangDetect] /detect timed out after %ss.", DETECT_TIMEOUT)
        return "en", 0.0
    except Exception as e:
        logger.error(f"[LangDetect] Unexpected error: {e}")
        return "en", 0.0


def translate_to_english(text: str, source_lang: str) -> str:
    # Same contract as detect_language: the fallback is the untouched text,
    # which is exactly what the ConnectionError handler already returns.
    if not ENABLED or _breaker_is_open():
        return text

    try:
        response = requests.post(
            TRANSLATE_ENDPOINT,
            json={"q": text, "source": source_lang, "target": TARGET_LANG},
            timeout=TRANSLATE_TIMEOUT
        )
        response.raise_for_status()
        translated = response.json().get("translatedText", "").strip()

        if not translated:
            logger.warning("[Translate] Empty translation returned, using original.")
            return text

        logger.info(f"[Translate] {source_lang}→en | original='{text[:60]}' | translated='{translated[:60]}'")
        return translated

    except requests.exceptions.ConnectionError:
        _trip_breaker()
        return text
    except requests.exceptions.Timeout:
        logger.error("[Translate] /translate timed out after %ss.", TRANSLATE_TIMEOUT)
        return text
    except Exception as e:
        logger.error(f"[Translate] Unexpected error: {e}")
        return text


def detect_and_translate(text: str) -> tuple[str, bool, str]:
    if not text or not text.strip():
        return text, False, "en"

    detected_lang, confidence = detect_language(text)

    if detected_lang == TARGET_LANG:
        logger.debug("[LangCheck] Detected English. No translation needed.")
        return text, False, detected_lang

    if confidence < MIN_CONFIDENCE:
        logger.warning(f"[LangCheck] Low confidence ({confidence:.2f}) for lang={detected_lang}. Skipping.")
        return text, False, detected_lang

    if detected_lang in SUPPORTED_SOURCE_LANGS:
        translated = translate_to_english(text, source_lang=detected_lang)
        return translated, True, detected_lang

    logger.warning(f"[LangCheck] Unsupported language: {detected_lang} (confidence={confidence:.2f}). Passing through.")
    return text, False, detected_lang