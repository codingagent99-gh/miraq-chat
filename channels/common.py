"""
Channel-neutral pieces shared by the WhatsApp and Instagram renderers.

The widget response is rich (cards, chips, typed actions). Messaging channels
are not, so this module reduces a turn to a small neutral shape — text,
product cards, reply options, link buttons — and each channel renderer only
decides how to lay that shape out within its own limits.

REPLY IDS
---------
Button/list/quick-reply titles are truncated to fit the channel (WhatsApp
buttons allow 20 chars), so the title cannot be sent back as the message.
The full intent rides in the id/payload instead, and routes/channel.py
decodes it:

    s:<text>     a suggestion chip  -> message = <text>
    c:<name>     a category         -> message = CATEGORY_QUERY_TEMPLATE
    p:<page>     "Show more"        -> re-run the last user query at <page>
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app_config import get_currency_symbol

SUPPORTED_CHANNELS = frozenset({"whatsapp", "instagram"})

# What a tapped category sends as the user's message. The widget's own category
# click text lives in the frontend; keep this in step with it.
CATEGORY_QUERY_TEMPLATE = "Show me {name}"

SHOW_MORE_TITLE = "Show more"

# Max id/payload length we emit. WhatsApp list rows cap at 200, buttons at 256,
# Instagram payloads at 1000 — 200 is safe everywhere.
_MAX_REPLY_ID = 200

# Actions that only make sense inside the widget (rep/bulk flows, cart buttons
# wired to widget-side handlers). Dropped silently on channels.
WIDGET_ONLY_ACTIONS = frozenset({
    "SHOW_BULK_ORDER_BUTTON",
    "SHOW_BULK_ORDER_CONFIRMATION",
    "SHOW_BULK_ADDRESS_CONFIRMATION",
    "SHOW_BULK_VARIANT_PROMPT",
    "SHOW_RECENTLY_ORDERED_BUTTON",
    "PROPOSE_CHECKOUT_ADDRESS",
})

MAX_ORDER_LINES = 5


# ── Neutral turn shape ────────────────────────────────────────────────────────

@dataclass
class ReplyOption:
    id: str
    title: str        # full, untruncated label; renderers truncate


@dataclass
class LinkButton:
    label: str
    url: str
    short_label: str = ""     # for buttons with tight limits (WhatsApp: 20)


@dataclass
class ProductCard:
    name: str
    price_line: str
    image: str
    url: str


@dataclass
class ChannelTurn:
    text: str                                   # markdown, as the widget gets it
    products: List[ProductCard] = field(default_factory=list)
    suggestions: List[ReplyOption] = field(default_factory=list)
    categories: List[ReplyOption] = field(default_factory=list)
    links: List[LinkButton] = field(default_factory=list)

    @property
    def options(self) -> List[ReplyOption]:
        return self.suggestions + self.categories


# ── Reply-id codec ────────────────────────────────────────────────────────────

def encode_reply_id(kind: str, value) -> str:
    return f"{kind}:{value}"[:_MAX_REPLY_ID]


def decode_reply_id(reply_id: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Returns (message, page). page is set only for "Show more", in which case
    message is None and the caller re-runs the previous query.
    Unknown ids return (None, None) so the caller can fall back to the text.
    """
    kind, sep, value = (reply_id or "").partition(":")
    if not sep or not value:
        return None, None
    if kind == "s":
        return value, None
    if kind == "c":
        return CATEGORY_QUERY_TEMPLATE.format(name=value), None
    if kind == "p" and value.isdigit():
        return None, int(value)
    return None, None


# ── Text helpers ──────────────────────────────────────────────────────────────

def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_LINK_RE      = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE      = re.compile(r"\*\*(.+?)\*\*", re.S)
_HEADING_RE   = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BR_RE        = re.compile(r"<br\s*/?>", re.I)
_TAG_RE       = re.compile(r"</?[a-zA-Z][^>]*>")
_BULLET_RE    = re.compile(r"^(\s*)[-*]\s+", re.M)


def _link_repl(m: "re.Match") -> str:
    label, url = m.group(1).strip(), m.group(2)
    return url if label == url else f"{label}: {url}"


def _flatten_tables(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if _TABLE_SEP_RE.match(line) and "-" in line:
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            line = " · ".join(c for c in cells if c)
        out.append(line)
    return "\n".join(out)


def _common_cleanup(text: str) -> str:
    text = (text or "").replace("\r\n", "\n")
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("&amp;", "&")
    text = _flatten_tables(text)
    text = _LINK_RE.sub(_link_repl, text)
    text = _BULLET_RE.sub(r"\1• ", text)
    return text


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def markdown_to_whatsapp(text: str) -> str:
    """WhatsApp: *bold*, _italic_, ~strike~. Links render as bare URLs."""
    text = _common_cleanup(text)
    text = _HEADING_RE.sub(lambda m: f"*{m.group(1).strip('*')}*", text)
    text = _BOLD_RE.sub(lambda m: f"*{m.group(1)}*", text)
    return _collapse_blank_lines(text)


def markdown_to_plain(text: str) -> str:
    """Instagram renders no formatting at all."""
    text = _common_cleanup(text)
    text = _HEADING_RE.sub(lambda m: m.group(1), text)
    text = _BOLD_RE.sub(lambda m: m.group(1), text)
    return _collapse_blank_lines(text)


def split_text(text: str, limit: int) -> List[str]:
    """Split on paragraph, then line, then hard boundaries; never exceeds limit."""
    text = (text or "").strip()
    if not text:
        return []
    chunks, current = [], ""

    def _pieces(block: str):
        if len(block) <= limit:
            yield block
            return
        for line in block.split("\n"):
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                yield line[:cut]
                line = line[cut:].lstrip()
            yield line

    for para in text.split("\n\n"):
        for i, piece in enumerate(_pieces(para)):
            sep = "\n\n" if i == 0 else "\n"
            candidate = f"{current}{sep}{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


# ── Response -> ChannelTurn ───────────────────────────────────────────────────

def _money(value, symbol: str) -> str:
    try:
        return f"{symbol}{float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def _product_card(p: dict, symbol: str) -> Optional[ProductCard]:
    name = (p.get("name") or "").replace("&amp;", "&").strip()
    if not name:
        return None
    price = _money(p.get("price"), symbol)
    if p.get("on_sale") and p.get("regular_price") and price:
        regular = _money(p.get("regular_price"), symbol)
        if regular and regular != price:
            price = f"{price} (was {regular})"
    stock = "In stock" if p.get("in_stock") else "Out of stock"
    price_line = " · ".join(x for x in (price, stock) if x)
    images = p.get("images") or []
    image = images[0] if images and isinstance(images[0], str) else ""
    return ProductCard(
        name=name,
        price_line=price_line,
        image=image if image.startswith("https://") else "",
        url=p.get("permalink") or "",
    )


def _order_lines(orders: list, symbol: str) -> str:
    lines = []
    for o in orders[:MAX_ORDER_LINES]:
        total = _money(o.get("total"), o.get("currency") or symbol)
        date = (o.get("date_created") or "")[:10]
        status = str(o.get("status") or "").replace("-", " ").title()
        parts = [f"#{o.get('order_number') or o.get('id')}", status, total, date]
        lines.append("• " + " · ".join(x for x in parts if x))
    if len(orders) > MAX_ORDER_LINES:
        lines.append(f"…and {len(orders) - MAX_ORDER_LINES} more")
    return "\n".join(lines)


def _top_sellers_text(payload: dict) -> str:
    blocks = []
    label = payload.get("window_label") or ""
    for g in payload.get("groups") or []:
        names = [(p.get("name") or "").strip() for p in g.get("products") or []]
        names = [n for n in names if n][:3]
        if names:
            blocks.append(f"**{g.get('name')}**\n" + "\n".join(f"- {n}" for n in names))
    if not blocks:
        return ""
    head = f"**Best sellers by collection ({label})**" if label else "**Best sellers by collection**"
    return head + "\n\n" + "\n\n".join(blocks)


_CHANNEL_NAMES = {"whatsapp": "WhatsApp", "instagram": "Instagram"}


def _short_channel_label(channel) -> str:
    name = _CHANNEL_NAMES.get(str(channel or "").lower())
    return f"Chat on {name}" if name else "Chat with us"


def build_turn(data: dict, max_products: int) -> ChannelTurn:
    symbol = get_currency_symbol()
    extra_text: List[str] = []
    suggestion_texts: List[str] = list(data.get("suggestions") or [])
    links: List[LinkButton] = []

    if data.get("orders"):
        extra_text.append(_order_lines(data["orders"], symbol))

    for action in data.get("actions") or []:
        a_type = (action or {}).get("type")
        payload = (action or {}).get("payload") or {}
        if a_type in WIDGET_ONLY_ACTIONS:
            continue
        if a_type == "SHOW_HUMAN_HANDOFF" and payload.get("url"):
            links.append(LinkButton(
                label=payload.get("label") or "Chat with us",
                url=payload["url"],
                short_label=_short_channel_label(payload.get("channel")),
            ))
        elif a_type == "SHOW_DATE_RANGE_PICKER":
            # Typed windows ("This month") are handled by handle_date_range_reply.
            suggestion_texts = list(payload.get("quick_options") or []) + suggestion_texts
        elif a_type == "SHOW_TOP_SELLERS_BY_COLLECTION":
            block = _top_sellers_text(payload)
            if block:
                extra_text.append(block)
        elif a_type == "SHOW_PRODUCT_RECENT_ORDERS":
            orders = payload.get("orders") or []
            if orders:
                extra_text.append(f"You've ordered this before ({len(orders)} recent order(s)).")

    text = "\n\n".join(x for x in [data.get("bot_message") or ""] + extra_text if x)
    # A link that is also sent as a button would show twice; keep the button.
    button_urls = {l.url for l in links}
    if button_urls:
        text = _LINK_RE.sub(lambda m: "" if m.group(2) in button_urls else m.group(0), text)

    products = [c for c in (_product_card(p, symbol) for p in data.get("products") or []) if c]

    seen = set()
    suggestions = []
    for s in suggestion_texts:
        s = str(s or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            suggestions.append(ReplyOption(encode_reply_id("s", s), s))

    pagination = data.get("pagination") or data.get("order_pagination") or {}
    if pagination.get("has_more"):
        next_page = int(pagination.get("page") or 1) + 1
        suggestions.insert(0, ReplyOption(encode_reply_id("p", next_page), SHOW_MORE_TITLE))

    categories = []
    for c in data.get("categories") or []:
        name = (c.get("name") or "").replace("&amp;", "&").strip()
        if name:
            categories.append(ReplyOption(encode_reply_id("c", name), name))

    return ChannelTurn(
        text=text,
        products=products[:max_products],
        suggestions=suggestions,
        categories=categories,
        links=links,
    )