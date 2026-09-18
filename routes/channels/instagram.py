"""
Instagram Messaging API renderer.

Output is a list of complete request bodies for
POST /{ig-user-id}/messages (graph.instagram.com / graph.facebook.com),
in send order:

    1. text chunks          (plain text — Instagram renders no markdown)
    2. product carousel     (generic template, up to 10 elements)
    3. link cards           (generic template with a web_url button)

Reply options go out as quick_replies on the LAST message, since Instagram
only shows the quick replies of the most recent message.

Limits enforced (Meta docs):
    text 1000 chars
    quick replies: max 13, title 20, payload 1000
    generic template: max 10 elements, title 80, subtitle 80, max 3 buttons,
                      button title 20
"""

from typing import List

from channels.common import (
    ChannelTurn, markdown_to_plain, split_text, truncate,
)

TEXT_LIMIT         = 1000
MAX_QUICK_REPLIES  = 13
QUICK_REPLY_TITLE  = 20
MAX_ELEMENTS       = 10
ELEMENT_TITLE      = 80
ELEMENT_SUBTITLE   = 80
BUTTON_TITLE       = 20

OPTIONS_PROMPT = "What would you like to do next?"


def _envelope(recipient: str, message: dict) -> dict:
    return {
        "recipient": {"id": recipient},
        "messaging_type": "RESPONSE",
        "message": message,
    }


def _template(elements: List[dict]) -> dict:
    return {"attachment": {"type": "template", "payload": {
        "template_type": "generic",
        "elements": elements,
    }}}


def _product_element(card) -> dict:
    el = {"title": truncate(card.name, ELEMENT_TITLE)}
    if card.price_line:
        el["subtitle"] = truncate(card.price_line, ELEMENT_SUBTITLE)
    if card.image:
        el["image_url"] = card.image
    if card.url:
        el["buttons"] = [{"type": "web_url", "url": card.url, "title": "View product"}]
    return el


def _link_element(link) -> dict:
    return {
        "title": truncate(link.label, ELEMENT_TITLE),
        "buttons": [{"type": "web_url", "url": link.url, "title": truncate(link.label if len(link.label) <= BUTTON_TITLE else (link.short_label or link.label), BUTTON_TITLE)}],
    }


def render_instagram(turn: ChannelTurn, recipient: str) -> List[dict]:
    messages: List[dict] = []

    for chunk in split_text(markdown_to_plain(turn.text), TEXT_LIMIT):
        messages.append({"text": chunk})

    if turn.products:
        messages.append(_template([_product_element(p) for p in turn.products[:MAX_ELEMENTS]]))

    if turn.links:
        messages.append(_template([_link_element(l) for l in turn.links[:MAX_ELEMENTS]]))

    options = turn.options[:MAX_QUICK_REPLIES]
    if options:
        if not messages:
            messages.append({"text": OPTIONS_PROMPT})
        messages[-1]["quick_replies"] = [
            {"content_type": "text", "title": truncate(o.title, QUICK_REPLY_TITLE), "payload": o.id}
            for o in options
        ]

    return [_envelope(recipient, m) for m in messages]