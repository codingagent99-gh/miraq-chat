"""
WhatsApp Cloud API renderer.

Output is a list of complete message objects for
POST /{phone-number-id}/messages, in send order:

    1. text chunks          (bot message, 4096 chars each)
    2. product cards        (image + caption, or text when there is no image)
    3. link buttons         (interactive cta_url, e.g. human handoff)
    4. reply options        (interactive buttons, or a list when they don't fit)

Limits enforced (Meta docs):
    text body 4096 · interactive body 1024 · image caption 1024
    reply buttons: max 3, title 20, id 256
    list: max 10 rows total, button 20, section title 24,
          row title 24, row description 72, row id 200
    cta_url display_text 20
"""

from typing import List

from channels.common import (
    ChannelTurn, ReplyOption, markdown_to_whatsapp, split_text, truncate,
)

TEXT_LIMIT          = 4096
INTERACTIVE_BODY    = 1024
CAPTION_LIMIT       = 1024
MAX_BUTTONS         = 3
BUTTON_TITLE        = 20
MAX_LIST_ROWS       = 10
LIST_BUTTON_TEXT    = "Choose an option"
ROW_TITLE           = 24
ROW_DESCRIPTION     = 72
SECTION_TITLE       = 24
CTA_TEXT            = 20

OPTIONS_PROMPT = "What would you like to do next?"
LINK_PROMPT    = "Tap below to continue."


def _envelope(to: str, msg_type: str, content: dict) -> dict:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": msg_type,
        msg_type: content,
    }


def _text(to: str, body: str) -> dict:
    return _envelope(to, "text", {"body": body, "preview_url": "http" in body})


def _product(to: str, card) -> dict:
    lines = [f"*{card.name}*"]
    if card.price_line:
        lines.append(card.price_line)
    if card.url:
        lines.append(card.url)
    caption = truncate("\n".join(lines), CAPTION_LIMIT)
    if card.image:
        return _envelope(to, "image", {"link": card.image, "caption": caption})
    return _text(to, caption)


def _cta_label(link) -> str:
    return link.label if len(link.label) <= CTA_TEXT else (link.short_label or link.label)


def _cta(to: str, link) -> dict:
    return _envelope(to, "interactive", {
        "type": "cta_url",
        "body": {"text": LINK_PROMPT},
        "action": {
            "name": "cta_url",
            "parameters": {"display_text": truncate(_cta_label(link), CTA_TEXT), "url": link.url},
        },
    })


def _buttons(to: str, body: str, options: List[ReplyOption]) -> dict:
    return _envelope(to, "interactive", {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": o.id, "title": o.title}}
            for o in options
        ]},
    })


def _rows(options: List[ReplyOption]) -> List[dict]:
    rows = []
    for o in options:
        row = {"id": o.id, "title": truncate(o.title, ROW_TITLE)}
        if len(o.title) > ROW_TITLE:
            row["description"] = truncate(o.title, ROW_DESCRIPTION)
        rows.append(row)
    return rows


def _list(to: str, body: str, turn: ChannelTurn) -> dict:
    # Suggestions (incl. "Show more") win the row budget; categories fill the rest.
    suggestions = turn.suggestions[:MAX_LIST_ROWS]
    categories = turn.categories[: MAX_LIST_ROWS - len(suggestions)]
    sections = []
    if categories:
        sections.append({"title": truncate("Categories", SECTION_TITLE), "rows": _rows(categories)})
    if suggestions:
        sections.append({"title": truncate("Options", SECTION_TITLE), "rows": _rows(suggestions)})
    if len(sections) == 1:
        # A single section doesn't need a title, and WhatsApp doesn't require one.
        sections[0].pop("title")
    return _envelope(to, "interactive", {
        "type": "list",
        "body": {"text": body},
        "action": {"button": LIST_BUTTON_TEXT, "sections": sections},
    })


def _fits_buttons(turn: ChannelTurn) -> bool:
    return (
        not turn.categories
        and 0 < len(turn.suggestions) <= MAX_BUTTONS
        and all(len(o.title) <= BUTTON_TITLE for o in turn.suggestions)
    )


def render_whatsapp(turn: ChannelTurn, to: str) -> List[dict]:
    messages: List[dict] = []
    chunks = split_text(markdown_to_whatsapp(turn.text), TEXT_LIMIT)

    # The last chunk becomes the interactive body when nothing is sent between
    # it and the options and it fits the smaller interactive limit.
    has_options = bool(turn.options)
    inline_body = None
    if (has_options and chunks and not turn.products and not turn.links
            and len(chunks[-1]) <= INTERACTIVE_BODY):
        inline_body = chunks.pop()

    messages.extend(_text(to, c) for c in chunks)
    messages.extend(_product(to, p) for p in turn.products)
    messages.extend(_cta(to, link) for link in turn.links)

    if has_options:
        body = inline_body or OPTIONS_PROMPT
        if _fits_buttons(turn):
            messages.append(_buttons(to, body, turn.suggestions))
        else:
            messages.append(_list(to, body, turn))

    return messages