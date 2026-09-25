"""
translation/turn.py — one chat turn in the customer's language.

    customer (mr) ──prepare_inbound()──▶ English pipeline ──@translate_reply──▶ customer (mr)

INBOUND  prepare_inbound(message, conversation, ...) is called in chat() once
         the conversation is loaded. It returns the English text the whole
         pipeline runs on, and records the turn's languages on flask.g.

OUTBOUND @translate_reply wraps the /chat view. Every return path of chat()
         — _finalize_turn, the early jsonify() exits, the crash handler —
         passes through it, so there is one place that translates
         bot_message and suggestion chips. It then persists:
           * the conversation's sticky language (context_data["_i18n_lang"])
           * a chip map (context_data["_i18n_chips"]): translated chip text ->
             English original. Tapping a translated chip sends the translated
             text back; the map turns it into the exact English string the
             flow handlers match on, with no lossy round-trip translation.
           * the translated reply on the stored bot Message's metadata, so
             /chat/history replays what the customer actually saw.

The DB keeps English (Message.content) because the pipeline and the LLM
fallback read history back as context.

Any failure here degrades to English. A translator outage never fails a turn.

WhatsApp/Instagram get all of this for free: routes/channel.py runs the same
/chat view in-process, so the decorator runs there too.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from flask import current_app, g
from sqlalchemy.orm.attributes import flag_modified

from chat_logger import get_logger, sanitize_log_string
from store_registry import get_tenant_features
from translation.base import TranslationUnavailable
from translation.registry import get_provider
from translation.script_detect import script_languages
from translation.settings import TranslationSettings, from_features
from translation.text import translate_inbound, translate_markdown, translate_texts

logger = get_logger("miraq_translation")

_G_KEY = "_miraq_turn_language"
CTX_LANG = "_i18n_lang"
CTX_CHIPS = "_i18n_chips"
_CHIP_MAP_MAX = 60
_VARIANT_STATE = "awaiting_variant_selection"
# English replies shorter than this don't switch a Marathi conversation back
# to English: "ok", "yes", "2" are answers, not a language choice.
_SWITCH_TO_EN_MIN_WORDS = 3


@dataclass
class TurnLanguage:
    settings: TranslationSettings
    conversation_id: object
    started_at: datetime
    input_lang: str = "en"
    reply_lang: str = "en"
    original_text: str = ""
    translated_in: bool = False
    chip_hit: bool = False
    sticky_before: Optional[str] = None
    extra: dict = field(default_factory=dict)


def _norm_chip(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _state() -> Optional[TurnLanguage]:
    try:
        return g.__dict__.get(_G_KEY)
    except RuntimeError:
        return None


def current_reply_language() -> str:
    """Language this turn's reply will be sent in ("en" when translation is off)."""
    st = _state()
    return st.reply_lang if st else "en"


# ── language decision ────────────────────────────────────────────────────────

def pick_input_language(text: str, settings: TranslationSettings, provider,
                         sticky: Optional[str]) -> str:
    """Which language the customer wrote in, restricted to what we can handle."""
    allowed = settings.languages
    preferred = [l for l in (sticky, settings.default_language) if l and l in allowed]

    for det in provider.detect(text):
        if det.confidence < settings.min_confidence:
            continue
        if det.language == "en" or det.language in allowed:
            return det.language
        break  # confident about a language this tenant doesn't serve

    # Detector unsure, or said something we don't serve (e.g. "hi" for a
    # Marathi-only store). The script narrows it: Devanagari text in a
    # Marathi store is Marathi.
    by_script = [l for l in script_languages(text) if l in allowed]
    if by_script:
        for l in preferred:
            if l in by_script:
                return l
        return by_script[0]

    if not re.search(r"[^\x00-\x7F]", text):
        return "en"  # plain ASCII the detector couldn't place
    return ""       # a language/script this tenant doesn't serve


# ── inbound ──────────────────────────────────────────────────────────────────

def prepare_inbound(message: str, conversation, *, flow_state: str = "",
                    payload_flow_state: str = "") -> str:
    """Return the English text for this turn. Never raises."""
    try:
        settings = from_features(get_tenant_features())
    except Exception:
        settings = TranslationSettings()
    if not settings.active:
        return message

    ctx = conversation.context_data or {}
    sticky = ctx.get(CTX_LANG) if ctx.get(CTX_LANG) in settings.languages else None
    st = TurnLanguage(
        settings=settings,
        conversation_id=conversation.id,
        started_at=datetime.now(timezone.utc),
        reply_lang=sticky or "en",
        original_text=message,
        sticky_before=sticky,
    )
    g.__dict__[_G_KEY] = st

    text = (message or "").strip()
    if not text:
        return message

    # A tapped chip: exact English original, no translation round-trip.
    chips = ctx.get(CTX_CHIPS) or {}
    english = chips.get(_norm_chip(text)) if isinstance(chips, dict) else None
    if english:
        st.chip_hit = True
        st.input_lang = sticky or "en"
        logger.info("[Translate] chip tap resolved | %r -> %r", text[:60], english[:60])
        return english

    # Structured control payloads (__DATE_RANGE__{...}, __bulk_order_trigger__)
    # and variant picks (catalogue values typed back verbatim) stay untouched.
    if text.startswith("__") or _VARIANT_STATE in (flow_state, payload_flow_state):
        return message

    provider = get_provider(settings.provider)
    if provider is None:
        logger.error("[Translate] unknown provider %r on tenant settings", settings.provider)
        return message

    try:
        lang = pick_input_language(text, settings, provider, sticky)
    except Exception as e:
        logger.error("[Translate] detection failed: %s", e)
        return message
    st.input_lang = lang or "und"

    if lang == "en":
        if len(text.split()) >= _SWITCH_TO_EN_MIN_WORDS:
            st.reply_lang = "en"
        return message
    if not lang:
        logger.info("[Translate] unsupported language, passing through | %r",
                    sanitize_log_string(text[:60]))
        return message

    st.reply_lang = lang
    try:
        english = translate_inbound(provider, text, lang, "en").strip()
    except TranslationUnavailable:
        return message  # pipeline gets the original; reply falls back to English below
    if not english:
        return message

    st.translated_in = True
    logger.info("[Translate] %s->en | %r -> %r", lang,
                sanitize_log_string(text[:60]), sanitize_log_string(english[:60]))
    return english


def user_message_i18n_metadata() -> dict:
    """metadata_json for the stored user Message: what the customer actually typed."""
    st = _state()
    if not st or not (st.translated_in or st.chip_hit):
        return {}
    return {"i18n": {"lang": st.input_lang, "original": st.original_text}}


# ── outbound ─────────────────────────────────────────────────────────────────

def _split_rv(rv):
    """(response, rest) for a Flask view return value, or (None, None)."""
    if isinstance(rv, tuple):
        return rv[0], rv[1:]
    return rv, ()


def _translate_payload(data: dict, st: TurnLanguage) -> Optional[dict]:
    """Translate bot_message + chips in place. Returns the i18n record, or None."""
    provider = get_provider(st.settings.provider)
    if provider is None:
        return None
    lang = st.reply_lang
    record = {"lang": lang}

    msg = data.get("bot_message")
    if isinstance(msg, str) and msg.strip():
        data["bot_message"] = translate_markdown(provider, msg, "en", lang)
        record["bot_message"] = data["bot_message"]

    # Variant chips are catalogue values ("Matte", 12"X24") — keep them exact.
    flow_state = data.get("flow_state") or (data.get("metadata") or {}).get("flow_state")
    sugg = data.get("suggestions")
    if flow_state != _VARIANT_STATE and isinstance(sugg, list):
        idx = [i for i, s in enumerate(sugg) if isinstance(s, str) and s.strip()
               and not s.startswith("__")]
        if idx:
            out = translate_texts(provider, [sugg[i] for i in idx], "en", lang)
            chip_map = {}
            for i, t in zip(idx, out):
                if t and t.strip() and t != sugg[i]:
                    chip_map[_norm_chip(t)] = sugg[i]
                    sugg[i] = t
            record["suggestions"] = list(sugg)
            record["chips"] = chip_map
    return record


def _persist(st: TurnLanguage, english_bot_message: Optional[str], record: Optional[dict]) -> None:
    from models import db, Conversation, Message

    try:
        conv = Conversation.query.get(st.conversation_id)
        if conv is None:
            return
        ctx = dict(conv.context_data or {})
        changed = False

        new_sticky = st.reply_lang if st.reply_lang != "en" else None
        if ctx.get(CTX_LANG) != new_sticky:
            if new_sticky:
                ctx[CTX_LANG] = new_sticky
            else:
                ctx.pop(CTX_LANG, None)
            changed = True

        if record and record.get("chips"):
            chips = dict(ctx.get(CTX_CHIPS) or {})
            chips.update(record["chips"])
            if len(chips) > _CHIP_MAP_MAX:
                chips = dict(list(chips.items())[-_CHIP_MAP_MAX:])
            ctx[CTX_CHIPS] = chips
            changed = True

        if changed:
            conv.context_data = ctx
            flag_modified(conv, "context_data")

        # Tag this turn's stored bot message (if the path stored one) with the
        # translated text. Matched by content + time so an early-return path
        # that saved nothing can't tag the previous turn's message.
        if record and english_bot_message is not None:
            bot = (
                Message.query
                .filter(Message.conversation_id == st.conversation_id,
                        Message.role == "bot",
                        Message.created_at >= st.started_at)
                .order_by(Message.id.desc())
                .first()
            )
            if bot is not None and bot.content == english_bot_message:
                meta = dict(bot.metadata_json or {})
                meta["i18n"] = {k: v for k, v in record.items() if k != "chips"}
                bot.metadata_json = meta
                flag_modified(bot, "metadata_json")
                changed = True

        if changed:
            db.session.commit()
    except Exception as e:
        logger.error("[Translate] could not persist turn language: %s", e, exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass


def translate_reply(view):
    """Decorator for the /chat view. Place directly under @chat_bp.route."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        rv = view(*args, **kwargs)
        st = _state()
        if st is None:
            return rv
        try:
            return _apply(rv, st)
        except Exception as e:
            logger.error("[Translate] reply translation failed, sending English: %s", e, exc_info=True)
            return rv

    return wrapper


def _apply(rv, st: TurnLanguage):
    resp, rest = _split_rv(rv)
    if not hasattr(resp, "get_json"):
        resp = current_app.make_response(resp)
    data = resp.get_json(silent=True)

    record = None
    english_msg = data.get("bot_message") if isinstance(data, dict) else None
    wants_translation = (
        isinstance(data, dict)
        and st.reply_lang != "en"
        and st.settings.translate_replies
    )
    if wants_translation:
        try:
            record = _translate_payload(data, st)
        except TranslationUnavailable:
            record = None  # English reply this turn; language stays sticky

    if isinstance(data, dict):
        data["language"] = st.reply_lang if record else "en"
        meta = data.get("metadata")
        if isinstance(meta, dict):
            meta["i18n"] = {
                "input_lang": st.input_lang,
                "reply_lang": data["language"],
                "translated_input": st.translated_in,
            }
        resp.set_data(json.dumps(data, ensure_ascii=False))
        resp.mimetype = "application/json"

    _persist(st, english_msg, record)
    return (resp, *rest) if rest else resp
