"""
language_utils.py
─────────────────
Detect language and translate to English using a locally hosted LibreTranslate instance.

Usage:
    from language_utils import detect_and_translate

    translated, was_translated, detected_lang = detect_and_translate("Hola, ¿cómo estás?")
    # → ("Hello, how are you?", True, "es")

    translated, was_translated, detected_lang = detect_and_translate("Show me products")
    # → ("Show me products", False, "en")
"""

import requests
import logging
from chat_logger import get_logger
import os
logger = get_logger("miraq_language")

# ── Config ──────────────────────────────────────────────────────────────────
PORT = int(os.getenv("LIBRE_TRANSLATE_PORT", 5000))
LIBRETRANSLATE_URL = f"http://localhost:{PORT}"   # Change if hosted on a different port
DETECT_ENDPOINT    = f"{LIBRETRANSLATE_URL}/detect"
TRANSLATE_ENDPOINT = f"{LIBRETRANSLATE_URL}/translate"

# Only translate if confidence is above this threshold.
# Prevents false positives on short/ambiguous text.
MIN_CONFIDENCE     = 0.5

# Target language is always English
TARGET_LANG        = "en"

# Languages we want to support translating FROM.
# Add more codes here if needed (e.g. "fr", "pt").
SUPPORTED_SOURCE_LANGS = {"es"}


# ── Core Functions ───────────────────────────────────────────────────────────

def detect_language(text: str) -> tuple[str, float]:
    """
    Calls LibreTranslate /detect and returns (language_code, confidence).
    Falls back to ("en", 0.0) on any error so the pipeline never crashes.
    """
    try:
        response = requests.post(
            DETECT_ENDPOINT,
            json={"q": text},
            timeout=5
        )
        response.raise_for_status()
        results = response.json()

        if not results:
            logger.warning("Language detection returned empty results.")
            return "en", 0.0

        top = results[0]
        lang       = top.get("language", "en")
        confidence = top.get("confidence", 0.0)

        logger.debug(f"[LangDetect] text='{text[:60]}' → lang={lang}, confidence={confidence:.2f}")
        return lang, confidence

    except requests.exceptions.ConnectionError:
        logger.error("[LangDetect] LibreTranslate is not reachable at %s", LIBRETRANSLATE_URL)
        return "en", 0.0
    except requests.exceptions.Timeout:
        logger.error("[LangDetect] LibreTranslate /detect timed out.")
        return "en", 0.0
    except Exception as e:
        logger.error(f"[LangDetect] Unexpected error: {e}")
        return "en", 0.0


def translate_to_english(text: str, source_lang: str) -> str:
    """
    Calls LibreTranslate /translate to convert text → English.
    Returns the original text on any error so the pipeline never crashes.
    """
    try:
        response = requests.post(
            TRANSLATE_ENDPOINT,
            json={
                "q":      text,
                "source": source_lang,
                "target": TARGET_LANG,
            },
            timeout=10
        )
        response.raise_for_status()
        result = response.json()

        translated = result.get("translatedText", "").strip()
        if not translated:
            logger.warning("[Translate] Got empty translation, using original text.")
            return text

        logger.info(
            f"[Translate] {source_lang}→en | original='{text[:60]}' | translated='{translated[:60]}'"
        )
        return translated

    except requests.exceptions.ConnectionError:
        logger.error("[Translate] LibreTranslate is not reachable at %s", LIBRETRANSLATE_URL)
        return text
    except requests.exceptions.Timeout:
        logger.error("[Translate] LibreTranslate /translate timed out.")
        return text
    except Exception as e:
        logger.error(f"[Translate] Unexpected error: {e}")
        return text


def detect_and_translate(text: str) -> tuple[str, bool, str]:
    """
    Main entry point.

    Returns:
        (processed_text, was_translated, detected_language)

        - processed_text:    Either the translated English text or the original.
        - was_translated:    True if translation was performed.
        - detected_language: The detected ISO language code (e.g. "es", "en").
    """
    if not text or not text.strip():
        return text, False, "en"

    detected_lang, confidence = detect_language(text)

    # Already English or unrecognised language — pass through
    if detected_lang == TARGET_LANG:
        logger.debug(f"[LangCheck] Detected English. No translation needed.")
        return text, False, detected_lang

    # Low-confidence detection — treat as English to be safe
    if confidence < MIN_CONFIDENCE:
        logger.warning(
            f"[LangCheck] Low confidence ({confidence:.2f}) for lang={detected_lang}. Skipping translation."
        )
        return text, False, detected_lang

    # Language is in our supported set — translate
    if detected_lang in SUPPORTED_SOURCE_LANGS:
        translated = translate_to_english(text, source_lang=detected_lang)
        return translated, True, detected_lang

    # Unsupported non-English language — log and pass through
    logger.warning(
        f"[LangCheck] Unsupported language detected: {detected_lang} (confidence={confidence:.2f}). Passing through."
    )
    return text, False, detected_lang