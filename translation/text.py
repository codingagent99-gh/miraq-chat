"""
translation/text.py — translate bot text without breaking what must not change.

Bot messages are markdown and full of catalogue data: product names in
**bold**, sizes like 12"X24", prices, order numbers, links. A translator
happily rewrites all of those ("12\"X24\"" -> "12 \"X24\"" is the exact bug
that made chat.py skip translation during variant selection). So before a
line goes to the provider, protected spans are swapped for placeholders and
restored afterwards:

    **bold**, `code`, [links](url), bare URLs, emails, "quoted text",
    and any token containing a digit (prices, sizes, SKUs, #order ids)

If the provider mangles a placeholder, that line is re-translated in pieces
(only the free text between protected spans) so nothing protected is lost.

Lines are translated separately, keeping list markers, headings and blank
lines exactly as they were. Table rows (|...|) are left alone — they are data.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import List, Sequence, Tuple

from chat_logger import get_logger
from translation.base import TranslationProvider, TranslationUnavailable

logger = get_logger("miraq_translation")

_PROTECT_RE = re.compile(
    r"\*\*[^*\n]+?\*\*"              # **bold**
    r"|`[^`\n]+`"                     # `code`
    r"|\[[^\]\n]*\]\([^)\s]+\)"       # [text](url)
    r"|https?://\S+"                  # bare URL
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"      # email
    r"|\"[^\"\n]{1,80}\""             # "quoted"
    r"|“[^”\n]{1,80}”"
    r"|\S*\d\S*"                      # any token with a digit
)
_PREFIX_RE = re.compile(r"^(\s*(?:[-*•>]\s+|#{1,6}\s+|\d+[.)]\s+)?[^\w\s\"“*`\[]*\s*)")
_HAS_LETTER_RE = re.compile(r"[^\W\d_]")
_PLACEHOLDER = "[{}]"
_PLACEHOLDER_RE = re.compile(r"\[(\d+)\]")


# ── small shared cache: suggestion chips repeat on almost every turn ─────────
_CACHE_MAX = 4000
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        val = _cache.get(key)
        if val is not None:
            _cache.move_to_end(key)
        return val


def _cache_put(key, val):
    with _cache_lock:
        _cache[key] = val
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def translate_texts(provider: TranslationProvider, texts: Sequence[str],
                    source: str, target: str) -> List[str]:
    """Batch translate with caching. Raises TranslationUnavailable."""
    out: List[str] = list(texts)
    todo_idx, todo_txt = [], []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            continue
        hit = _cache_get((provider.name, source, target, t))
        if hit is not None:
            out[i] = hit
        else:
            todo_idx.append(i)
            todo_txt.append(t)
    if todo_txt:
        # Dedupe inside the batch too.
        uniq = list(dict.fromkeys(todo_txt))
        translated = provider.translate_batch(uniq, source, target)
        mapping = dict(zip(uniq, translated))
        for i, t in zip(todo_idx, todo_txt):
            val = mapping.get(t) or t
            out[i] = val
            _cache_put((provider.name, source, target, t), val)
    return out


# ── bot message ──────────────────────────────────────────────────────────────

def _mask(line: str) -> Tuple[str, List[str]]:
    kept: List[str] = []

    def repl(m):
        kept.append(m.group(0))
        return _PLACEHOLDER.format(len(kept))

    return _PROTECT_RE.sub(repl, line), kept


def _unmask(text: str, kept: List[str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: kept[int(m.group(1)) - 1], text)


def _placeholders_intact(text: str, n: int) -> bool:
    found = [int(x) for x in _PLACEHOLDER_RE.findall(text)]
    return sorted(found) == list(range(1, n + 1))


def translate_markdown(provider: TranslationProvider, text: str,
                       source: str, target: str) -> str:
    """Translate a bot message line by line. Raises TranslationUnavailable."""
    if not text or not text.strip():
        return text

    lines = text.split("\n")
    jobs = []  # (line_index, prefix, masked_body, kept)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("|") or not _HAS_LETTER_RE.search(stripped):
            continue
        prefix = _PREFIX_RE.match(line).group(1)
        body = line[len(prefix):]
        masked, kept = _mask(body)
        if not _HAS_LETTER_RE.search(_PLACEHOLDER_RE.sub("", masked)):
            continue  # nothing translatable left (e.g. a line that is only a product name)
        jobs.append((i, prefix, masked, kept))

    if not jobs:
        return text

    translated = translate_texts(provider, [j[2] for j in jobs], source, target)

    retry = []
    for (i, prefix, masked, kept), out in zip(jobs, translated):
        if not kept:
            lines[i] = prefix + out
        elif _placeholders_intact(out, len(kept)):
            lines[i] = prefix + _unmask(out, kept)
        else:
            retry.append((i, prefix, masked, kept))

    for i, prefix, masked, kept in retry:
        # Provider mangled a placeholder: translate only the free text between
        # protected spans, and stitch the protected spans back in verbatim.
        logger.debug("[Translate] placeholder lost, translating line %d in pieces", i)
        parts = re.split(r"(\[\d+\])", masked)
        free_idx = [k for k, p in enumerate(parts)
                    if not _PLACEHOLDER_RE.fullmatch(p) and _HAS_LETTER_RE.search(p)]
        pieces = translate_texts(provider, [parts[k].strip() for k in free_idx], source, target)
        for k, piece in zip(free_idx, pieces):
            lead = " " if parts[k][:1].isspace() else ""
            trail = " " if parts[k][-1:].isspace() else ""
            parts[k] = f"{lead}{piece}{trail}"
        lines[i] = prefix + _unmask("".join(parts), kept)

    return "\n".join(lines)


__all__ = ["translate_texts", "translate_markdown", "TranslationUnavailable"]
