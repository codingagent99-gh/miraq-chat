"""
language_utils.py
─────────────────
Detect language and translate to English using a locally hosted LibreTranslate instance.
"""

import requests
from chat_logger import get_logger
import os

logger = get_logger("miraq_language")

# ── Config ───────────────────────────────────────────────────────────────────
PORT               = int(os.getenv("LIBRE_TRANSLATE_PORT", 5012))  # ← matches PM2 config
LIBRETRANSLATE_URL = f"http://localhost:{PORT}"
DETECT_ENDPOINT    = f"{LIBRETRANSLATE_URL}/detect"
TRANSLATE_ENDPOINT = f"{LIBRETRANSLATE_URL}/translate"

MIN_CONFIDENCE         = 0.5
TARGET_LANG            = "en"
SUPPORTED_SOURCE_LANGS = {"es"}

# Detect is fast; translate can be slow on cold start (model lazy-loads)
DETECT_TIMEOUT    = 15   # seconds
TRANSLATE_TIMEOUT = 60   # seconds


# ── Core Functions ────────────────────────────────────────────────────────────

def detect_language(text: str) -> tuple[str, float]:
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
        return lang, confidence

    except requests.exceptions.ConnectionError:
        logger.error("[LangDetect] LibreTranslate not reachable at %s", LIBRETRANSLATE_URL)
        return "en", 0.0
    except requests.exceptions.Timeout:
        logger.error("[LangDetect] /detect timed out after %ss.", DETECT_TIMEOUT)
        return "en", 0.0
    except Exception as e:
        logger.error(f"[LangDetect] Unexpected error: {e}")
        return "en", 0.0


def translate_to_english(text: str, source_lang: str) -> str:
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
        logger.error("[Translate] LibreTranslate not reachable at %s", LIBRETRANSLATE_URL)
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