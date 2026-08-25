"""
parsers/bulk_order_parser.py — Parse free-text bulk order utterances into
resolved BulkOrderLine objects.

No Flask. WooCommerce API calls only (via woo_client / endpoints).

Public API:
    parse_bulk_order_utterance(text, store_loader, role, self_customer_id)
        → List[BulkOrderLine]
"""

import re
import difflib
from dataclasses import dataclass, field
from typing import Optional, List

from woo_client import woo_client
from ecommerce import endpoints
from chat_logger import get_logger
from app_config import BULK_ORDER_FULL_SCOPE_ROLES, BULK_ORDER_ROLES, ECOMMERCE_BACKEND
from handlers.chat_utils import normalize_spelling_variants, _attribute_display_name, variation_declares_self_contained_term, _normalize_term_key
from models import ExtractedEntities
from classifier.extractors import extract_attributes
from api_builder.store_helpers import resolve_attr_filters
from api_builder.filter_builder import build_advanced_filter_call

logger = get_logger("miraq_chat")

# ── Company roster pagination ────────────────────────────────────────────────
# The plugin hard-caps per_page at 20 (min(20, ...) in
# get_customers_by_company), so a company's full membership can only be read
# by paging. MAX_ROSTER_PAGES bounds that walk: 5 pages × 20 = up to 100
# contacts per company. Past that we stop and mark the roster truncated
# rather than silently treating a partial list as complete.
ROSTER_PAGE_SIZE = 20
MAX_ROSTER_PAGES = 5

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', re.I)
# ══════════════════════════════════════════════════════════════
# DATACLASS
# ══════════════════════════════════════════════════════════════

@dataclass
class BulkOrderLine:
    raw_fragment: str
    company_name: str                     # transaction-wide company scope
    recipient_name: str                   # person this line ships to (within the company)
    email: str                            # customer identifier; empty string if not provided
    product_name: str
    quantity: int
    quantity_explicitly_set: bool
    product_id: Optional[int]
    variation_id: Optional[int]
    customer_id: Optional[str]
    customer_display_name: str
    is_self_order: bool
    shipping_address: Optional[dict]
    billing_address: Optional[dict]
    is_reorder: bool
    reorder_source_order_id: Optional[int]
    unresolved: bool
    unresolved_reason: Optional[str]
    unmatched_variant_hint: str = ""   # hint the user typed that matched no variation
    blank_variant_axes: list = field(default_factory=list)  # axes the matched variation leaves as "Any"
    candidate_variation_ids: list = field(default_factory=list)  # hint matched several variations
    specified_variant_axes: list = field(default_factory=list)   # axes the matched variation DOES set
    self_contained_variant: bool = False  # Chip Card etc — other axes deliberately N/A
    variant_meta: dict = field(default_factory=dict)  # axes the parser pinned itself (chip-card fallback)

# ══════════════════════════════════════════════════════════════
# INTERNAL: intermediate pre-line structure
# ══════════════════════════════════════════════════════════════

@dataclass
class _PreLine:
    raw_fragment: str
    company_name: str
    recipient_name: str
    email: str
    product_name: str
    quantity: int
    is_reorder: bool
    quantity_explicitly_set: bool = False
    product_id: Optional[int] = None
    customer_id: Optional[str] = None
    reorder_source_order_id: Optional[int] = None
    variant_hint: str = ""
    variation_id: Optional[int] = None
    unmatched_variant_hint: str = ""
    blank_variant_axes: list = field(default_factory=list)
    candidate_variation_ids: list = field(default_factory=list)
    specified_variant_axes: list = field(default_factory=list)
    self_contained_variant: bool = False
    variant_meta: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════
# PUBLIC FUNCTION
# ══════════════════════════════════════════════════════════════

class MultipleCompaniesError(ValueError):
    """Raised when one utterance names more than one company.

    The requirement restricts a bulk order to a single company per
    transaction, so this is rejected outright rather than partially applied.
    """

    def __init__(self, companies):
        self.companies = companies
        super().__init__(f"multiple companies in one request: {companies}")


# "for company Beck LTD" — the explicit, unambiguous form.
_FOR_COMPANY_RE = re.compile(
    r'\bfor\s+company\s+([^,]+)'        # "for company Gensler"
    r'|\bfor\s+([^,]+?)\s+company\b',   # "for Gensler company"
    re.I,
)

# Both orderings are in live use. Only the keyword-first one was recognised,
# so "order allspice chipcard for gensler company" carried no company scope at
# all — the tail was stripped as noise and the rep was then asked who the order
# was for, having just said so in the message.

# Text that names a company EXPLICITLY — a scope marker plus the word
# "company", or a trailing "at <name>". Deliberately excludes a bare
# "for <name>" tail: that is genuinely ambiguous between a person and a
# company, and is resolved later against the roster rather than guessed at
# classification time.
#
# BulkOrderEvaluator imports this. Same single-source-of-truth reason as
# COMPANY_SCOPE_TAIL_RE below: a marker added here must not need a second
# edit somewhere else to take effect.
EXPLICIT_COMPANY_SCOPE_RE = re.compile(
    r'\bfor\s+company\s+[^,]+'
    r'|\bfor\s+[^,]+?\s+company\b'
    r'|\bat\s+[^,]+$',
    re.I,
)

# Any "for <something>" tail on a fragment.
_FOR_TAIL_RE = re.compile(r'\bfor\s+(.+)$', re.I)

# Spelled-out counts accepted before "each" ("order one each of A and B").
# Ordered longest-first where prefixes overlap so the alternation cannot match
# a shorter word inside a longer one. Stops at twelve deliberately: past that,
# people type digits, and every extra word is another chance to swallow a
# product name.
_WORD_NUMERALS = {
    "twelve": 12, "eleven": 11, "seven": 7, "eight": 8, "three": 3,
    "nine": 9, "four": 4, "five": 5, "six": 6, "ten": 10, "two": 2,
    "one": 1,
}

# ── Company-scope markers ────────────────────────────────────────────────────
# The bulk-order format scopes a company with one of these words:
#     "... for Claire at Abel Design Group"
#     "... Aurora Taupe for Beck LTD"
#
# "on" is deliberately NOT a marker: "Order Harmony Moon, Adams Grey on sale"
# would read "sale" as the company.
#
# SINGLE SOURCE OF TRUTH. routes/chat.py imports COMPANY_SCOPE_TAIL_RE from
# here to decide which tokens the typo corrector must leave alone. Keeping two
# copies is what let "at Beck" get silently corrected to "at back" after "at"
# was added here but not there — every marker added below is protected from
# typo correction automatically.
COMPANY_SCOPE_MARKERS = ("for", "at")

# "at" tails, anchored to the fragment end — this marks a company only in
# trailing position ("Adams Grey at Beck"), never mid-fragment.
_AT_COMPANY_RE = re.compile(r'\bat\s+([^,]+)$', re.I)

# Any scope tail, used ONLY for typo-guard token extraction (not for parsing).
# Unanchored so it catches "for ram" mid-fragment as well as trailing tails.
COMPANY_SCOPE_TAIL_RE = re.compile(
    r'\b(?:for(?:\s+company)?|at)\s+([^,]+)', re.I
)


def _extract_company_scope(text: str):
    """
    Pull the transaction-wide company out of the raw utterance.

    Handles the explicit forms only ("... for company Beck LTD" and
    "... for Beck LTD company"); the implicit trailing form ("Order A, B, C
    for Beck LTD") is resolved later, once the text has been split into
    fragments and we can tell a lone trailing "for" from per-line recipients.

    Returns (company_name, cleaned_text).
    Raises MultipleCompaniesError if two different companies are named.
    """
    names = []
    # Two alternatives, so findall yields a tuple per match — exactly one
    # group is non-empty.
    for groups in _FOR_COMPANY_RE.findall(text):
        raw = next((g for g in groups if g), "")
        name = raw.strip().strip(' ,.')
        if name and not any(name.lower() == seen.lower() for seen in names):
            names.append(name)

    if len(names) > 1:
        raise MultipleCompaniesError(names)

    if names:
        cleaned = _FOR_COMPANY_RE.sub('', text).strip().strip(' ,.')
        return names[0], cleaned

    return "", text


def parse_bulk_order_utterance(
    text: str,
    store_loader,
    role: str = "",
    self_customer_id: Optional[str] = None,
    meta_out: Optional[dict] = None,
) -> List[BulkOrderLine]:
    """
    Parse a free-text bulk order utterance into a list of resolved BulkOrderLine objects.

    Steps:
      1. Split into fragments (comma / " and <digit>")
      2. Per-fragment extraction (qty, company, product, reorder flag)
      3. Product resolution (store_loader, no API call)
      4. Company resolution (one API call per unique company, batched; rep only)
      5. Reorder resolution (API call per reorder line with resolved customer)
      6. Assemble final BulkOrderLine objects
    """
    # Company/recipient-scoped resolution for the bulk-order flow: reps (and
    # cs/pm roles) plus "customer" — a customer bulk-ordering to a named
    # recipient/company still goes through real resolution, not just
    # self-scoped cart behavior. See BULK_ORDER_FULL_SCOPE_ROLES. This governs
    # whether the company/recipient PARSING machinery below (Steps 0/1.5/1.6/2)
    # runs at all — it must run for "customer" too, so a customer's "for
    # Ashlynn at Beck LTD" gets parsed the same way a rep's does.
    _is_rep = role in BULK_ORDER_FULL_SCOPE_ROLES
    # _is_true_rep: the ORIGINAL rep-tier roles only (unchanged set). These
    # roles have no self-order fallback — they must always resolve to a named
    # company/recipient, exactly as before this change. "customer" is
    # deliberately NOT in this set: a customer who names nothing at all
    # (no company, no recipient, no email) is ordering for themselves, same
    # as their pre-existing self-checkout behavior — see the self-fallback in
    # Step 6 below, gated on `not _is_true_rep`.
    _is_true_rep = role in BULK_ORDER_ROLES

    # Shopify has no company/recipient-roster backend at all — ShopifyEndpoints
    # implements neither search_customers_by_company nor
    # search_customers_by_email (the DynamicEndpointsRouter would raise
    # AttributeError trying to dispatch either). True rep roles never reach
    # this deployment (the widget can only send role="customer"/"guest" — see
    # the backstop in routes/chat.py), so this was never exercised before.
    # "customer" now being in BULK_ORDER_FULL_SCOPE_ROLES changes that: force
    # full self-scoped behavior on Shopify for every role EXCEPT a true rep,
    # so a Shopify customer's "for Ashlynn at Beck LTD" is silently ignored
    # (stripped as noise, same as any other unrecognised text) exactly as it
    # already was before this change, instead of crashing. True reps are left
    # alone here since that path is unreachable in practice today.
    if _is_rep and not _is_true_rep and ECOMMERCE_BACKEND == "shopify":
        _is_rep = False

    # ── Step 0: Transaction-wide company scope ("... for company Beck LTD") ──
    # Bulk orders are scoped to ONE company; naming two raises immediately.
    company_scope = ""
    if _is_rep:
        company_scope, text = _extract_company_scope(text)

    # ── Step 1: Split into fragments ─────────────────────────────────────────
    _catalog_names = {
        p["name"].lower() for p in (store_loader.products or []) if p.get("name")
    }

    # Longest-name-first product list — used in Step 2 to pre-claim a catalog
    # product name from a fragment before the qty scanner runs, so multi-word
    # / versioned names (e.g. "Aura 2.0") aren't split apart by quantity logic.
    _products_by_name_len = sorted(
        (p for p in (store_loader.products or []) if p.get("name")),
        key=lambda p: len(p["name"]),
        reverse=True,
    )

    # Pass 1: split on commas
    raw_parts = re.split(r',\s*', text)

    # Pass 2: within each comma-part, split on "and" if both sides
    # resolve to catalog products OR if "and" precedes a digit (existing logic).
    # This handles: "A and B", "A, B and C", "A and B and C".
    final_fragments = []
    for part in raw_parts:
        # Always split "and" before a digit (original behaviour)
        digit_split = re.split(r'\s+and\s+(?=\d)', part)
        expanded = []
        for sub in digit_split:
            if re.search(r'\band\b', sub, re.I):
                and_parts = [p.strip() for p in re.split(r'\s+and\s+', sub, flags=re.I) if p.strip()]
                resolved = sum(
                    1 for p in and_parts
                    if any(name in p.lower() for name in _catalog_names)
                )
                if resolved >= 2:
                    expanded.extend(and_parts)
                    continue
            expanded.append(sub)
        final_fragments.extend(expanded)

    # ── Step 1.5: Implicit trailing company ──────────────────────────────────
    # Three shapes reach here, all without the explicit "for company" keyword:
    #
    #   (a) "Order A, B, C for Beck LTD."
    #       Only the LAST fragment has a "for" tail, so it scopes the whole
    #       order rather than naming a recipient for C alone.
    #
    #   (b) "Order A for ram, B for sovan for Abel Design Group"
    #       The last fragment has TWO "for" tails — the first names the
    #       recipient, the LAST names the company.
    #
    #   (c) "Order A for Ashlynn, B for Claire at Abel Design Group"
    #       "at" separates person from company. This reads most naturally to
    #       a rep and was previously swallowed whole into the recipient
    #       ("Claire at Abel Design Group"), so no company was ever detected.
    if _is_rep and not company_scope:
        # (c) first — an explicit "at" marker is less ambiguous than counting
        # "for" tails, so it wins when present.
        _at_hits = []
        for i, frag in enumerate(final_fragments):
            _m = _AT_COMPANY_RE.search(frag)
            if not _m:
                continue
            _name = _m.group(1).strip().strip(' ,.')
            if _name:
                _at_hits.append((i, _m.start(), _name))

        _distinct_at = []
        for _, _, _name in _at_hits:
            if not any(_name.lower() == seen.lower() for seen in _distinct_at):
                _distinct_at.append(_name)
        if len(_distinct_at) > 1:
            raise MultipleCompaniesError(_distinct_at)

        if _at_hits:
            company_scope = _distinct_at[0]
            # Strip the "at <company>" tail from every fragment carrying it.
            for i, start, _ in _at_hits:
                final_fragments[i] = final_fragments[i][:start].strip().strip(' ,.')
            logger.debug(
                f"bulk_parser | scope-marker company → '{company_scope}'"
            )

    if _is_rep and not company_scope:
        _last = final_fragments[-1] if final_fragments else ""
        _for_positions = [m.start() for m in re.finditer(r'\bfor\b', _last, re.I)]
        _for_idx = [
            i for i, f in enumerate(final_fragments)
            if _FOR_TAIL_RE.search(f)
        ]

        _split_at = None
        if len(_for_positions) >= 2:
            # (b) last "for" in the final fragment starts the company scope
            _split_at = _for_positions[-1]
        elif len(_for_idx) == 1 and _for_idx[0] == len(final_fragments) - 1:
            # (a) the lone "for" tail across the whole utterance
            _split_at = _for_positions[0] if _for_positions else None

        if _split_at is not None:
            _tail = _last[_split_at:]
            _m = _FOR_TAIL_RE.search(_tail)
            if _m:
                _cand = EMAIL_RE.sub('', _m.group(1)).strip().strip(' ,.')
                # An email tail identifies a person, not a company.
                if _cand and not EMAIL_RE.search(_m.group(1)):
                    company_scope = _cand
                    final_fragments[-1] = _last[:_split_at].strip()
                    logger.debug(
                        f"bulk_parser | trailing company scope → '{company_scope}'"
                    )

    # ── Step 1.6: Transaction-wide RECIPIENT and "each" quantity ─────────────
    # "Order 1 chip card each of Harmony, Adams, Marigold, Lager for Kiki at
    # Gensler" means all four go to Kiki, one each. But the "for" tail and the
    # quantity both sit on single fragments — the tail on the last, the number
    # on the first — so per-fragment extraction gave Kiki only the Lager and
    # left three lines with no recipient and no quantity. A live order split
    # 3/1 between the wrong person and the right one because of this.
    #
    # Company scope already works transaction-wide (Step 0/1.5); these are the
    # same idea for the other two shared values.
    recipient_scope = ""
    each_quantity = None
    if _is_rep and len(final_fragments) > 1:
        _tails = [
            i for i, f in enumerate(final_fragments)
            if _FOR_TAIL_RE.search(f or "")
        ]
        # ONLY when the last fragment is the sole one carrying a "for" tail.
        # If several fragments name their own people ("Harmony for ram, Adams
        # for sovan"), each tail is that line's own recipient and nothing is
        # shared — that is the documented multi-recipient shape and must not
        # be collapsed onto one person.
        if len(_tails) == 1 and _tails[0] == len(final_fragments) - 1:
            _m = _FOR_TAIL_RE.search(final_fragments[-1])
            _cand = EMAIL_RE.sub('', _m.group(1)).strip().strip(' ,.')
            if _cand:
                recipient_scope = _cand
                logger.debug(
                    f"bulk_parser | shared recipient scope → '{recipient_scope}' "
                    f"(applied to {len(final_fragments)} lines)"
                )

    # "N <unit> each of A, B, C" — the count distributes over every line rather
    # than belonging to the fragment that happens to carry the digits. Scanned
    # on the ORIGINAL text, since by now the number sits in fragment 0 only.
    #
    # Word numerals count too: "order one each of …" and "two each of …" are
    # at least as common as digits here, and matching only \d+ would leave
    # "two each" silently distributing 1 (the per-fragment default) — wrong in
    # a way nothing downstream would flag, since a quantity of 1 looks
    # deliberate rather than missing.
    if re.search(r'\beach\b', text, re.I):
        _each_m = re.search(
            r'\b(\d+|' + '|'.join(_WORD_NUMERALS) + r')\b(?=[^,]*\beach\b)',
            text, re.I,
        )
        if _each_m:
            _tok = _each_m.group(1).lower()
            each_quantity = int(_tok) if _tok.isdigit() else _WORD_NUMERALS[_tok]
            logger.debug(f"bulk_parser | 'each' quantity → {each_quantity} per line")

    # ── Step 2: Per-fragment extraction ──────────────────────────────────────
    pre_lines: List[_PreLine] = []

    for fragment in final_fragments:
        fragment = fragment.strip()
        if not fragment:
            continue

        # ── Pre-claim a catalog product name (longest match wins) so the
        # qty scanner never sees its tokens. Fixes cases like "order aura 2.0"
        # where \b matches between '2' and '.', causing qty to be misread
        # as 2 from the ".0" suffix of a versioned product name.
        _pre_match_span = None
        for _p in _products_by_name_len:
            _m = re.search(r'\b' + re.escape(_p["name"]) + r'\b', fragment, re.I)
            if _m:
                _pre_match_span = _m.span()
                break

        if _pre_match_span:
            qty_scan_text = fragment[:_pre_match_span[0]] + fragment[_pre_match_span[1]:]
        else:
            qty_scan_text = fragment

        qty_match = re.search(r'\b(\d+)\b', qty_scan_text)
        quantity = int(qty_match.group(1)) if qty_match else 1
        quantity_explicitly_set = qty_match is not None

        # "1 … each of A, B, C" — a fragment that named no count of its own
        # inherits the shared one rather than silently defaulting to 1 and
        # then prompting for a quantity the user already gave.
        if each_quantity is not None and not quantity_explicitly_set:
            quantity = each_quantity
            quantity_explicitly_set = True

        is_reorder = bool(
            re.search(r'\b(reorder|re-order|last\s+week[\'s]*|previous)\b', fragment, re.I)
        )

        # ── Email extraction (rep only — non-rep orders on their own account) ──
        email = ""
        if _is_rep:
            email_match = EMAIL_RE.search(fragment)
            if email_match:
                email = email_match.group(0).strip()

        # ── Recipient: the "for <person>" tail, minus any email ──────────────
        # The company is transaction-wide (Step 0/1.5), so anything left after
        # "for" on an individual fragment names the PERSON the line ships to.
        recipient_name = ""
        if _is_rep:
            for_match = _FOR_TAIL_RE.search(fragment)
            if for_match:
                candidate = EMAIL_RE.sub('', for_match.group(1)).strip().strip(', ')
                if candidate:
                    recipient_name = candidate
            # A trailing "for <person>" on the LAST fragment covers the whole
            # order (Step 1.6) — apply it to fragments that named nobody.
            if not recipient_name and recipient_scope:
                recipient_name = recipient_scope

        # ── Product: strip quantity, email addresses, and "for …" tail ──
        # If a catalog product name was pre-claimed above, leave the fragment
        # intact instead — slicing at qty_match.end() here could cut into or
        # past the claimed product name (e.g. a leading "20 " before it),
        # whereas Step 3's matching logic (3a/3b/3b.5) can find it cleanly
        # in the unsliced text.
        product_part = fragment
        if qty_match and _pre_match_span is None:
            product_part = product_part[qty_match.end():].strip()
        product_part = EMAIL_RE.sub('', product_part).strip()
        product_part = re.sub(r'\s*\bfor\b.*$', '', product_part, flags=re.I).strip()
        
        # Strip leading intent phrase + order verb — anchored so it never
        # strips "order" appearing mid-string (e.g. "harmony order confirmation")
        product_part = re.sub(
            r'^(?:(?:i\s+(?:want|need|would\s+like)\s+to|please|can\s+you)\s+)?'
            r'(?:order|buy|purchase|reorder|re-order)\s+',
            '',
            product_part,
            flags=re.I,
        ).strip()
        product_name = product_part.strip(" ,.-")

        pre_lines.append(_PreLine(
            raw_fragment=fragment,
            company_name=company_scope,
            recipient_name=recipient_name,
            email=email,
            product_name=product_name,
            quantity=quantity,
            quantity_explicitly_set=quantity_explicitly_set,
            is_reorder=is_reorder,
        ))

    if not pre_lines:
        return []

    # ── Step 2.5: Transaction-wide LEADING quantity ───────────────────────────
    # "Order 1 Harmony, Lager, Adams, Marigold chip card for Gensler" — the
    # only explicit digit ("1") sits on the FIRST fragment ("1 Harmony").
    # Every other fragment defaults to quantity=1 anyway, so this happens to
    # look right for qty=1 — but "Order 5 Harmony, Lager, Adams, Marigold
    # chip card for Gensler" would silently give Harmony qty=5 and leave the
    # rest at the default 1, wrong in exactly the way nothing downstream
    # would flag (a quantity of 1 looks deliberate, not missing).
    #
    # Same shape as the "each" distribution above, just without the word
    # "each" to key off. Mirrors its conservative gate: only fires when
    # exactly ONE fragment has an explicit quantity and it is the FIRST
    # fragment (the natural position for a leading shared count) — so it
    # never touches the documented multi-recipient shape ("20 Harmony White
    # for ram, 15 Adams Grey for sovan"), where every fragment already
    # carries its own quantity, and never fires when "each" already handled
    # distribution (every fragment is already quantity_explicitly_set there).
    _explicit_qty = [pl for pl in pre_lines if pl.quantity_explicitly_set]
    _implicit_qty = [pl for pl in pre_lines if not pl.quantity_explicitly_set]
    if (
        len(_explicit_qty) == 1 and _implicit_qty
        and _explicit_qty[0] is pre_lines[0]
    ):
        _shared_qty = _explicit_qty[0].quantity
        for pl in _implicit_qty:
            pl.quantity = _shared_qty
            pl.quantity_explicitly_set = True
        logger.debug(
            f"bulk_parser | shared leading quantity → {_shared_qty} "
            f"(applied to {len(_implicit_qty)} line(s) lacking their own)"
        )

    # ── Step 3: Product resolution + variant hint extraction ──────────────────
    for pl in pre_lines:
        if not store_loader or not pl.product_name:
            continue

        products = store_loader.products or []
        matched_catalog_name = None

        # 3a. Exact match (case-insensitive) — full product_name
        for p in products:
            if p.get("name", "").lower() == pl.product_name.lower():
                pl.product_id = p["id"]
                matched_catalog_name = p["name"]
                break

        # 3b. First-word exact match: "Harmony White" → try "Harmony"
        if pl.product_id is None:
            first_word = pl.product_name.split()[0] if pl.product_name.split() else ""
            if first_word:
                for p in products:
                    if p.get("name", "").lower() == first_word.lower():
                        pl.product_id = p["id"]
                        matched_catalog_name = p["name"]
                        break

        # 3b.5. Substring scan: find the longest catalog name contained within
        # product_name. Safety net for any verb-prefixed text that slipped
        # through Step 2 cleaning (e.g. "i want to order saga" → "Saga").
        #
        # "Longest" is a tie-break, not evidence, and it picks the wrong
        # product whenever one product's name appears inside another's
        # VARIATION name: "Aurora - Thunder Black" matches both Aurora (6
        # chars) and Thunder (7), so Thunder won, the hint was reduced to
        # "Black", and Thunder has no Black. Same defect as the one in
        # get_product_for_text, in a second cascade — fixing that one alone
        # left this path still resolving to Thunder, because the parser
        # re-resolves each line itself and never consults the entities the
        # classifier already got right.
        #
        # So collect every match first and let the catalog decide: the Colors
        # term in the text ("Aurora - Thunder Black") is prefixed by exactly
        # one candidate. Falls through to the length rule untouched whenever
        # the term evidence does not single one out.
        if pl.product_id is None:
            _named = [
                p for p in products
                if p.get("name")
                and re.search(r'\b' + re.escape(p["name"]) + r'\b', pl.product_name, re.I)
            ]

            if len(_named) > 1 and hasattr(store_loader, "narrow_by_attribute_term"):
                _pick = store_loader.narrow_by_attribute_term(
                    pl.product_name.lower(),
                    [
                        {
                            "name": p.get("name", ""),
                            "slug": p.get("slug", ""),
                            "numeric_id": p.get("id"),
                        }
                        for p in _named
                    ],
                )
                if _pick:
                    logger.debug(
                        f"bulk_parser | attribute term narrowed "
                        f"{[p.get('name') for p in _named]} → {_pick['name']}"
                    )
                    _named = [p for p in _named if p.get("id") == _pick["numeric_id"]]

            if _named:
                _chosen = max(_named, key=lambda x: len(x.get("name", "")))
                pl.product_id = _chosen["id"]
                matched_catalog_name = _chosen["name"]

        # 3c. Fuzzy fallback (cutoff=0.6)
        if pl.product_id is None:
            product_names = [p.get("name", "") for p in products]
            matches = difflib.get_close_matches(
                pl.product_name, product_names, n=1, cutoff=0.6
            )
            if matches:
                matched_catalog_name = matches[0]
                pl.product_id = next(
                    (p["id"] for p in products if p.get("name") == matched_catalog_name),
                    None,
                )

        # 3c.5. Attribute-based fallback: the line may describe a product by
        # its ATTRIBUTES rather than its name (e.g. "Grey Marble" = colour
        # grey + visual marble) — 3a-3c only ever compare against catalog
        # product NAMES, so a pure attribute description can never match
        # there and always fell through to unresolved. Reuse the exact same
        # attribute vocabulary and filter-resolution the main product search
        # flow uses, and query the advanced filter endpoint for it. Only
        # auto-resolve on a SINGLE match — several matches means the
        # description is genuinely ambiguous, and guessing one would be
        # worse than asking (same reasoning as the unmatched_variant_hint
        # safeguard above).
        if pl.product_id is None:
            _attr_entities = ExtractedEntities()
            extract_attributes(pl.product_name, _attr_entities)
            _attr_filters = resolve_attr_filters(_attr_entities.attributes) if _attr_entities.attributes else {}
            if _attr_filters:
                _attr_call = build_advanced_filter_call(
                    attributes=_attr_filters,
                    page=1, per_page=5,
                    in_stock=True,
                    description=f"Bulk order attribute resolve: '{pl.product_name}'",
                )

                # Backend dispatch. The call body is identical either way —
                # build_advanced_filter_call emits taxonomies in Woo-shaped
                # form ("pa_color") and the Shopify executor strips the
                # "pa_" prefix itself (_taxonomy_match), so no filter
                # rewriting is needed.
                #
                # ShopifyGraphQLExecutor, deliberately, and NOT the in-memory
                # ShopifyQueryExecutor: routes/chat.py:1047 runs ordinary
                # product search through the GraphQL one, and if this used a
                # different engine then "matte grey" could resolve to one
                # product when searched and a different one (or nothing)
                # when bulk-ordered. Same engine, same answer.
                _attr_ok = False
                _attr_products = []
                if ECOMMERCE_BACKEND == "shopify":
                    try:
                        from api_builder.shopify_graphql_executor import (
                            ShopifyGraphQLExecutor,
                        )
                        _attr_data = ShopifyGraphQLExecutor(
                            store_loader
                        ).execute_from_body(_attr_call.body)
                        _attr_products = (_attr_data or {}).get("products", [])
                        _attr_ok = True
                    except Exception as exc:
                        # Mirrors the Woo failure branch: leave the line
                        # unresolved so the shopper is asked, rather than
                        # guessing a product from a query that never ran.
                        logger.warning(
                            f"bulk_parser | shopify attribute resolve failed for "
                            f"'{pl.product_name}' | error={exc}"
                        )
                else:
                    _attr_result = woo_client.execute(_attr_call)
                    _attr_data = _attr_result.get("data") or {}
                    _attr_products = (
                        _attr_data.get("products", [])
                        if isinstance(_attr_data, dict) else []
                    )
                    _attr_ok = bool(_attr_result.get("success"))

                if _attr_ok and len(_attr_products) == 1:
                    matched_catalog_name = _attr_products[0].get("name")
                    pl.product_id = _attr_products[0].get("id")
                    logger.debug(
                        f"bulk_parser | attribute-resolved '{pl.product_name}' → "
                        f"'{matched_catalog_name}' (id={pl.product_id}) "
                        f"via {_attr_entities.attributes}"
                    )
                elif _attr_ok and len(_attr_products) > 1:
                    logger.info(
                        f"bulk_parser | attribute match for '{pl.product_name}' "
                        f"({_attr_entities.attributes}) is ambiguous — "
                        f"{len(_attr_products)} products match, leaving unresolved"
                    )

        # 3d. Extract variant hint OR display company from remainder, and
        # normalize product_name to the canonical catalog name. Normalization
        # now fires whenever a match was found — not just when product_name
        # starts with it — because the Step 2 pre-claim can leave a leading
        # qty token in product_name (e.g. "20 harmony white").
        if matched_catalog_name:
            # Search for the matched name rather than requiring product_name
            # to START with it: Step 2 deliberately skips the qty-slice when
            # a name was pre-claimed (to protect the pre-claim span), so a
            # stray leading token can survive here (e.g. "1 london white" for
            # "London White" after "1" failed to get stripped). A prefix
            # check against that would silently drop everything after the
            # match as an unrecognised variant hint — searching for the
            # match's position instead finds "White" regardless of what
            # precedes "London".
            _name_match = re.search(
                r'\b' + re.escape(matched_catalog_name) + r'\b', pl.product_name, re.I
            )
            if _name_match:
                remainder = pl.product_name[_name_match.end():].strip(" ,.-")
                if remainder:
                    _for_match = re.match(r'^for\s+(.+)$', remainder, re.I)
                    if _for_match:
                        # "for <Name>" in remainder names the RECIPIENT; the
                        # company is transaction-wide and set in Step 0/1.5.
                        if not pl.recipient_name:
                            candidate = EMAIL_RE.sub('', _for_match.group(1)).strip().strip(', ')
                            if candidate:
                                pl.recipient_name = candidate
                    else:
                        pl.variant_hint = remainder
                else:
                    # Nothing follows the product name — try the LEADING text
                    # instead. "1 each chip card from Harmony" puts the hint
                    # BEFORE the name, not after: strip the quantity token,
                    # "each"/"every"/"per", and a trailing connector word
                    # immediately before the name, and whatever survives is
                    # the hint. Only meaningful once qty/each are stripped
                    # away — a bare leading "1 " alone is not a hint.
                    _lead = pl.product_name[:_name_match.start()]
                    _lead = re.sub(
                        r'^\s*(?:\d+|' + '|'.join(_WORD_NUMERALS) + r')\b',
                        '', _lead, flags=re.I,
                    )
                    _lead = re.sub(r'\b(each|every|per)\b', '', _lead, flags=re.I)
                    _lead = re.sub(r'\b(from|of)\s*$', '', _lead, flags=re.I)
                    _lead = _lead.strip(" ,.-")
                    if _lead:
                        pl.variant_hint = _lead
                        logger.debug(
                            f"bulk_parser | leading variant hint → '{_lead}' "
                            f"(before product name '{matched_catalog_name}')"
                        )
            pl.product_name = matched_catalog_name
            
        if pl.product_id:
            logger.debug(
                f"bulk_parser | resolved product '{pl.product_name}' → id={pl.product_id} "
                f"variant_hint='{pl.variant_hint}'"
            )
        else:
            logger.debug(
                f"bulk_parser | unresolved product '{pl.product_name}'"
            )

    # ── Step 3.7: Transaction-wide shared VARIANT HINT (leading or trailing) ──
    # "Order 1 Harmony, Lager, Adams, Marigold chip card for Gensler" — the
    # hint trails on the LAST fragment ("Marigold chip card").
    # "Order 1 each chip card from Harmony, Lager, Adams, Marigold" — the
    # hint leads on the FIRST fragment instead ("chip card from Harmony").
    # Same underlying shape as the shared-recipient and each-quantity
    # problems above either way. Without this, only the one fragment that
    # textually touches the hint gets a variant_hint and the rest are left
    # with none, so they fall through to individual colour/finish prompts
    # even though the hint plainly reads as covering the whole list.
    #
    # Conservative by design, same as Step 1.6: only fires when exactly ONE
    # resolved line has a hint, every other resolved line has none, AND the
    # hinted line is the FIRST or LAST one — the natural positions for a
    # shared descriptor. A genuinely mixed set ("Harmony White for X, Adams
    # chip card for Y") must NOT be collapsed — each line already carries
    # its own hint and this block leaves those alone.
    _hinted = [pl for pl in pre_lines if pl.product_id and pl.variant_hint]
    _unhinted = [pl for pl in pre_lines if pl.product_id and not pl.variant_hint]
    if (
        len(_hinted) == 1 and _unhinted and pre_lines
        and (_hinted[0] is pre_lines[-1] or _hinted[0] is pre_lines[0])
    ):
        _shared_hint = _hinted[0].variant_hint
        for pl in _unhinted:
            pl.variant_hint = _shared_hint
        logger.debug(
            f"bulk_parser | shared variant hint → '{_shared_hint}' "
            f"(applied to {len(_unhinted)} line(s) lacking their own)"
        )

    # ── Step 3.5: Variation resolution (API call per unique product with a hint) ─
    _variant_cache: dict = {}   # product_id → list[variation dicts]; avoids duplicate calls
    _variant_fetch_failed: set = set()   # product_ids whose lookup errored out

    for pl in pre_lines:
        if not pl.product_id or not pl.variant_hint:
            continue

        if pl.product_id not in _variant_cache and pl.product_id not in _variant_fetch_failed:
            # Backend-neutral: Woo fetches over REST, Shopify reads the
            # variants the store loader already holds. Going through
            # woo_client.execute() directly here meant this lookup was
            # refused outright on a Shopify deployment (by design — see the
            # guard in woo_client), so every hinted line fell into
            # _variant_fetch_failed and prompted.
            #
            # None means "could not determine" and [] means "genuinely none";
            # only the former belongs in _variant_fetch_failed.
            data = endpoints.list_variants_resolved(
                product_id=pl.product_id,
                store_loader=store_loader,
                per_page=100,
            )
            if data is None:
                # A transport failure used to return {"success": False,
                # "data": []}, which is indistinguishable from "this product
                # has no variations" once cached. Track it separately so a
                # dropped connection is never reported to the rep as "that
                # option isn't in the catalog".
                _variant_fetch_failed.add(pl.product_id)
                logger.warning(
                    f"bulk_parser | variation lookup FAILED for product_id="
                    f"{pl.product_id} | hint='{pl.variant_hint}'"
                )
            else:
                _variant_cache[pl.product_id] = data

        if pl.product_id in _variant_fetch_failed:
            # Leave variation_id unset so the variant prompt still fires, but
            # do NOT claim the hint was not found — we never got to check.
            continue

        hint_lower = normalize_spelling_variants(pl.variant_hint)
        _matches = []
        for var in _variant_cache[pl.product_id]:
            _attr_list = var.get("attributes", [])
            if isinstance(_attr_list, dict):
                _options = list(_attr_list.values())
            else:
                _options = [a.get("option", "") for a in _attr_list if isinstance(a, dict)]
            # Normalise BOTH sides — this catalog mixes US and UK spellings
            # ("ADAMS Gray" vs "Aurora - Misty Grey").
            if any(hint_lower in normalize_spelling_variants(o) for o in _options):
                _matches.append(var)
                continue
            # Whitespace/hyphen-insensitive fallback so "chipcard" and
            # "chip-card" reach the catalog's "Chip Card" the same way
            # "chip card" already does.
            if any(
                _normalize_term_key(hint_lower) in _normalize_term_key(o)
                for o in _options
                if str(o).strip()
            ):
                _matches.append(var)

        if len(_matches) == 1:
            pl.variation_id = _matches[0]["id"]
        elif len(_matches) > 1:
            # The hint narrows but does not identify: "Harmony Moon" exists
            # in several sizes/finishes. Taking the first match silently
            # picked one for the rep. Leave variation_id unset so the
            # variant prompt fires, and remember which variations are still
            # in play so the prompt only offers the axes still open.
            pl.candidate_variation_ids = [v["id"] for v in _matches if v.get("id")]
            logger.info(
                f"bulk_parser | hint {pl.variant_hint!r} matches "
                f"{len(_matches)} variations of product {pl.product_id} — "
                f"will ask"
            )

        if pl.variation_id:
            # A self-contained sample form (Chip Card) is complete on its own:
            # the variation deliberately leaves the other axes as WooCommerce
            # "Any", so they must NOT be treated as missing and prompted for.
            _resolved_var = next(
                (v for v in _variant_cache[pl.product_id] if v.get("id") == pl.variation_id),
                None,
            )
            if _resolved_var and variation_declares_self_contained_term(_resolved_var):
                pl.self_contained_variant = True
                logger.info(
                    f"bulk_parser | product {pl.product_id} variation "
                    f"{pl.variation_id} is a self-contained sample form — "
                    f"other axes suppressed, will not prompt"
                )

        if pl.variation_id:
            # A matched variation can still be only PARTIALLY specified: this
            # catalog has products (Adams) where every variation carries a
            # colour but leaves Finish and Sample Size blank — WooCommerce
            # "Any". The storefront still makes the shopper choose those, so
            # record them and let the handler ask rather than silently
            # ordering an under-specified line.
            #
            # This branch must stay paired with the `else` below, which records
            # a hint we could NOT match: gating the whole branch on
            # `not self_contained_variant` sent Chip Card lines into that else
            # and stamped unmatched_variant_hint='chip card' on a line whose
            # variation had in fact resolved. Only the blank-axis collection is
            # suppressed for a self-contained form.
            _matched = next(
                (v for v in _variant_cache[pl.product_id] if v.get("id") == pl.variation_id),
                None,
            )
            _attrs = (_matched or {}).get("attributes", [])
            _blank = []
            if isinstance(_attrs, dict):
                _blank = [k for k, v in _attrs.items() if not str(v or "").strip()]
            elif isinstance(_attrs, list):
                _blank = [
                    a.get("name") or a.get("slug") or ""
                    for a in _attrs
                    if isinstance(a, dict) and not str(a.get("option") or "").strip()
                ]
            pl.blank_variant_axes = [] if pl.self_contained_variant else [
                _attribute_display_name(k) for k in _blank if k
            ]
            pl.specified_variant_axes = [
                _attribute_display_name(k)
                for k, v in (
                    _attrs.items() if isinstance(_attrs, dict)
                    else [(a.get("name") or a.get("slug") or "", a.get("option"))
                          for a in _attrs if isinstance(a, dict)]
                )
                if k and str(v or "").strip()
            ]
            if pl.blank_variant_axes:
                logger.info(
                    f"bulk_parser | variation {pl.variation_id} leaves "
                    f"{pl.blank_variant_axes} unset — will prompt"
                )

            logger.debug(
                f"bulk_parser | resolved variation hint='{pl.variant_hint}' "
                f"→ variation_id={pl.variation_id}"
            )
        else:
            # A chip card asked for on a product that has NO dedicated
            # chip-card variation (Elizabeth Mosaic, London: every variation
            # pins a Colour and nothing pins Sample Size).
            #
            # A chip card is the same physical card whatever colour the
            # variation names, so the colour/finish carry no meaning here —
            # asking the rep to choose one is a question with no real answer.
            # Pin the line to the first variation, record Chip Card as the
            # sample size, and suppress the prompts the same way a dedicated
            # chip-card variation does.
            #
            # This is a workaround for catalog data: the correct fix is a
            # chip-card variation per product with the other axes set to N/A.
            # Keep it gated on SELF_CONTAINED_VARIATION_TERMS so it only ever
            # fires for the terms declared there, never for an ordinary
            # unmatched hint like "Taupe".
            _hint_norm = _normalize_term_key(pl.variant_hint or "")
            _self_terms = {}
            try:
                from config.store_config import SELF_CONTAINED_VARIATION_TERMS
                _self_terms = SELF_CONTAINED_VARIATION_TERMS or {}
            except Exception:
                _self_terms = {}
            _wanted = [
                (axis, _normalize_term_key(t), t)
                for axis, terms in _self_terms.items()
                for t in (terms or [])
            ]
            _match = next(
                ((axis, term_name) for axis, term_norm, term_name in _wanted
                 if term_norm and term_norm == _hint_norm),
                None,
            )
            _pool = _variant_cache.get(pl.product_id) or []

            if _match and _pool:
                _axis, _term_name = _match
                _first = _pool[0]
                pl.variation_id = _first.get("id")
                pl.self_contained_variant = True
                pl.blank_variant_axes = []
                # Carry the sample size explicitly — it is the one axis that
                # actually describes what ships, and without it the line is
                # indistinguishable from an ordinary colour sample.
                #
                # The CONFIGURED term name, not the user's spelling: someone
                # typing "chipcard" must not put "Chipcard" on the order while
                # the catalog and every other line say "Chip Card".
                pl.variant_meta = dict(getattr(pl, "variant_meta", None) or {})
                pl.variant_meta.setdefault(
                    _attribute_display_name(_axis), _term_name
                )
                logger.info(
                    f"bulk_parser | product {pl.product_id} has no dedicated "
                    f"chip-card variation — pinning to variation {pl.variation_id} "
                    f"and recording {_axis} = {_term_name!r}; other axes carry no "
                    f"meaning for a chip card, so they are not prompted"
                )
            else:
                # Remember WHAT the user asked for so the variant prompt can say
                # "I couldn't find Taupe" instead of silently asking them to pick.
                pl.unmatched_variant_hint = pl.variant_hint
                logger.debug(
                    f"bulk_parser | unresolved variation hint='{pl.variant_hint}' "
                    f"for product_id={pl.product_id}"
                )

    # ── Step 4: Customer resolution by email (rep only, batched) ──────────────
    # Non-rep users have no email values, so unique_emails is empty
    # and this step is a no-op for them.
    email_resolution_cache: dict = {}   # email -> resolved dict | None

    unique_emails = list({pl.email for pl in pre_lines if pl.email})

    for email in unique_emails:
        call = endpoints.search_customers_by_email(
            email=email,
            per_page=1,
            description=f"Bulk order email lookup: '{email}'",
        )
        result = woo_client.execute(call)

        customers = result.get("data", [])
        if not result.get("success") or not isinstance(customers, list) or not customers:
            logger.debug(f"bulk_parser | email '{email}' → not found")
            email_resolution_cache[email] = None
            continue

        customer = customers[0]
        company_field = customer.get("company") or customer.get("billing", {}).get("company", "")
        full_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        display = company_field or full_name or f"Customer #{customer['id']}"

        email_resolution_cache[email] = {
            "id": str(customer["id"]),
            "display": display,
            "billing": customer.get("billing", {}),
            "shipping": customer.get("shipping", {}),
        }
        logger.debug(f"bulk_parser | email '{email}' → id={customer['id']} display='{display}'")

    # Stamp customer_id onto pre_lines so Step 5 (reorder) can use it
    for pl in pre_lines:
        if not pl.email:
            continue
        resolution = email_resolution_cache.get(pl.email)
        if resolution:
            pl.customer_id = resolution["id"]

    # ── Step 4b: Company roster resolution (rep only, one API call) ───────────
    # The company scopes the whole transaction, so one lookup serves every
    # line. Each line's recipient is then matched WITHIN that roster.
    company_roster: List[dict] = []
    company_lookup_done = False
    # True when we stopped paging with more records still possibly unread, so
    # downstream code must NOT assert that a missing name is absent from the
    # company — it is only absent from what we actually looked at.
    roster_truncated = False

    if _is_rep and company_scope:
        _seen_ids = set()
        for _page in range(1, MAX_ROSTER_PAGES + 1):
            call = endpoints.search_customers_by_company(
                company_name=company_scope,
                per_page=ROSTER_PAGE_SIZE,
                page=_page,
                requesting_customer_id=self_customer_id,
                description=f"Bulk order company lookup: '{company_scope}' (page {_page})",
            )
            result = woo_client.execute(call)
            company_lookup_done = True

            _raw_data = result.get("data", [])
            customers = _raw_data
            if isinstance(customers, dict):
                customers = customers.get("results", []) or customers.get("customers", [])
            if not (result.get("success") and isinstance(customers, list)):
                # A failed page mid-walk means the roster is incomplete —
                # flag it rather than treating what we have as the whole list.
                if _page > 1:
                    roster_truncated = True
                else:
                    logger.warning(
                        f"bulk_parser | company lookup failed for '{company_scope}' "
                        f"on page {_page}"
                    )
                break

            # The plugin returns a {_diagnostic: {...}} envelope rather than a
            # bare [] when nothing matched, so "no such company" can be told
            # apart from an out-of-date plugin build without server access.
            if isinstance(_raw_data, dict) and _raw_data.get("_diagnostic"):
                # The plugin returns this envelope for "no rows on this page",
                # which is also what the page AFTER a full page looks like. Only
                # the first page having no rows means the company is unknown;
                # later pages just mark the end of the roster, and warning there
                # made a successful lookup read as a failure in the logs.
                if company_roster:
                    logger.debug(
                        f"bulk_parser | company lookup end of roster at page "
                        f"{_page} | {len(company_roster)} customer(s) so far"
                    )
                else:
                    logger.warning(
                        f"bulk_parser | company lookup NO MATCHES | "
                        f"diagnostic={_raw_data['_diagnostic']}"
                    )
                break

            _page_rows = [c for c in customers if isinstance(c, dict) and c.get("id")]
            if _page_rows and _page == 1:
                logger.info(
                    f"bulk_parser | roster page 1 | lookup_version="
                    f"{_page_rows[0].get('lookup_version', 'MISSING -> OLD PLUGIN BUILD')}"
                    f" | companies={[r.get('company') for r in _page_rows[:5]]}"
                )
            for c in _page_rows:
                # Defensive dedupe: an unstable server-side sort could repeat a
                # row across pages, which would read as a false "two people
                # with this name" and trigger a bogus disambiguation prompt.
                if c["id"] not in _seen_ids:
                    _seen_ids.add(c["id"])
                    company_roster.append(c)

            # A short page means we reached the end of the result set.
            if len(customers) < ROSTER_PAGE_SIZE:
                break
            # Walked the full allowance and the last page was still full —
            # there are probably more customers we never fetched.
            if _page == MAX_ROSTER_PAGES:
                roster_truncated = True

        logger.info(
            f"bulk_parser | company '{company_scope}' → "
            f"{len(company_roster)} customer(s)"
            + (
                f" (TRUNCATED at {MAX_ROSTER_PAGES} pages — more may exist)"
                if roster_truncated else ""
            )
        )

    def _roster_entry_to_resolution(customer: dict) -> dict:
        full_name = (
            f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        )
        # Some accounts carry a junk name where first and last are the same word
        # (Andrew_Gazda@gensler.com is stored as "Gazda Gazda"), which showed the
        # rep a name nobody recognises. Billing usually holds the real one.
        #
        # This is deliberately narrow: billing is NOT a better source in general —
        # eleanor_baker@gensler.com bills as "Jennifer McKinney", and preferring
        # billing outright would rename her in the picker. Only a degenerate or
        # empty account name falls back, and only to a billing name that is
        # actually different and non-empty.
        _first = str(customer.get("first_name", "") or "").strip()
        _last = str(customer.get("last_name", "") or "").strip()
        if not full_name or (_first and _first.casefold() == _last.casefold()):
            _bill = customer.get("billing", {}) or {}
            _bill_name = (
                f"{_bill.get('first_name', '')} {_bill.get('last_name', '')}".strip()
            )
            if _bill_name and _bill_name.casefold() != full_name.casefold():
                logger.debug(
                    f"bulk_parser | customer {customer.get('id')} account name "
                    f"'{full_name}' looks degenerate — using billing name "
                    f"'{_bill_name}'"
                )
                full_name = _bill_name
        display = full_name or customer.get("email") or f"Customer #{customer['id']}"
        _ship = customer.get("shipping", {}) or {}
        _bill = customer.get("billing", {}) or {}
        _addr = _ship if _ship.get("address_1") else _bill
        return {
            "id": str(customer["id"]),
            "display": display,
            # Two people can share a name inside one company — these are what
            # tell them apart in the picker.
            "email": customer.get("email", "") or "",
            "city": _addr.get("city", "") or "",
            "state": _addr.get("state", "") or "",
            "address_1": _addr.get("address_1", "") or "",
            "billing": _bill,
            "shipping": _ship,
        }

    def _match_recipient(name: str):
        """Match a person name against the company roster.

        Returns (resolution | None, match_count).

        Resolves ONLY on a unique match. Zero matches and several matches both
        return None — the caller tells them apart by match_count, and several
        becomes an "ambiguous" prompt rather than a silent first-match pick.
        """
        if not name or not company_roster:
            return None, 0
        needle = re.sub(r'[^a-z0-9]+', ' ', name.lower()).strip()
        if not needle:
            return None, 0

        matches = []
        for c in company_roster:
            first = str(c.get("first_name", "") or "").lower()
            last = str(c.get("last_name", "") or "").lower()
            full = f"{first} {last}".strip()
            email_local = str(c.get("email", "") or "").split("@")[0].lower()
            haystacks = {h for h in (first, last, full, email_local) if h}
            if any(needle == h for h in haystacks) or any(
                needle in h.split() for h in haystacks
            ):
                matches.append(c)

        if not matches:
            return None, 0
        if len(matches) > 1:
            # Same name, two records — e.g. one person on file at two of the
            # company's sites, each with its own address. Picking the first
            # would silently ship to the wrong one, so refuse to guess and let
            # the handler disambiguate.
            logger.info(
                f"bulk_parser | recipient '{name}' matches {len(matches)} "
                f"records at '{company_scope}' — ambiguous, will ask"
            )
            return None, len(matches)
        return _roster_entry_to_resolution(matches[0]), 1

    # Stamp company-resolved customers onto pre_lines (email still wins if given)
    for pl in pre_lines:
        if not _is_rep or pl.customer_id:
            continue
        if pl.recipient_name:
            resolution, _count = _match_recipient(pl.recipient_name)
            if resolution:
                pl.customer_id = resolution["id"]
        elif len(company_roster) == 1 and not roster_truncated:
            # No person named and the company has exactly one contact —
            # unambiguous, so use them (example query 1). Gated on a COMPLETE
            # roster: "exactly one" read off a partial list is not a fact
            # about the company, and silently shipping to that person would
            # be the worst possible failure here.
            pl.customer_id = _roster_entry_to_resolution(company_roster[0])["id"]

    if meta_out is not None:
        # The caller needs the roster to build a recipient picker; re-querying
        # it in the handler would cost a second identical API round trip.
        meta_out["company_scope"] = company_scope
        meta_out["company_roster"] = [
            _roster_entry_to_resolution(c) for c in company_roster
        ]
        # Lets the handler soften "I couldn't find X" into "not in the first N"
        # instead of asserting an absence it cannot actually vouch for.
        meta_out["company_roster_truncated"] = roster_truncated

    # ── Step 5: Reorder resolution ────────────────────────────────────────────
    for pl in pre_lines:
        if not pl.is_reorder or not pl.customer_id:
            continue

        call = endpoints.list_rep_orders(
            body={"customer_id": pl.customer_id, "per_page": 3},
            description="Fetch recent orders for reorder resolution",
        )
        result = woo_client.execute(call)

        orders = result.get("data", [])
        if isinstance(orders, dict):
            orders = orders.get("orders", [])

        source_order_id = None
        for order in orders:
            for item in order.get("line_items", []):
                pid_match = (
                    pl.product_id and item.get("product_id") == pl.product_id
                )
                name_match = (
                    pl.product_name.lower() in item.get("name", "").lower()
                )
                if pid_match or name_match:
                    source_order_id = order["id"]
                    # Backfill product_id from order history if still unresolved
                    if not pl.product_id:
                        pl.product_id = item.get("product_id")
                    break
            if source_order_id:
                break

        pl.reorder_source_order_id = source_order_id
        logger.debug(
            f"bulk_parser | reorder for customer_id={pl.customer_id} "
            f"product='{pl.product_name}' → source_order_id={source_order_id}"
        )

    # ── Step 6: Assemble final BulkOrderLine objects ──────────────────────────
    result_lines: List[BulkOrderLine] = []

    for pl in pre_lines:
        _recipient_matches = 0
        # A "customer" (full-scope but not a true rep — see _is_true_rep)
        # who named nothing at all for THIS line — no email, no
        # transaction-wide company scope, no per-line recipient — is
        # ordering for themselves, exactly as before "customer" was added to
        # BULK_ORDER_FULL_SCOPE_ROLES. True reps get no such fallback: they
        # must always resolve to a named company/recipient (unchanged). The
        # moment a customer names ANY of those three signals, they fall
        # through to the same real resolution a rep gets — that's the whole
        # point of this expansion.
        _customer_self_fallback = (
            _is_rep and not _is_true_rep
            and not pl.email and not company_scope and not pl.recipient_name
        )
        if _is_rep and not _customer_self_fallback:
            resolution = email_resolution_cache.get(pl.email) if pl.email else None

            # Fall back to the company roster when no email was given.
            if not resolution and company_roster:
                if pl.recipient_name:
                    resolution, _recipient_matches = _match_recipient(pl.recipient_name)
                    # _recipient_matches > 1 means several people share the
                    # name — handled as "ambiguous", not "not found".
                elif len(company_roster) == 1:
                    resolution = _roster_entry_to_resolution(company_roster[0])

            if resolution:
                customer_id = resolution["id"]
                customer_display_name = resolution["display"]
                is_self_order = False
                shipping_address = resolution.get("shipping") or {}
                billing_address = resolution.get("billing") or {}
                if not shipping_address.get("address_1"):
                    shipping_address = billing_address
            else:
                customer_id = None
                is_self_order = False
                shipping_address = None
                billing_address = None
                if pl.email:
                    customer_display_name = "⚠️ Not found"
                elif not company_scope:
                    customer_display_name = "⚠️ Company required"
                elif not company_roster:
                    customer_display_name = f"⚠️ No customers for {company_scope}"
                elif pl.recipient_name and _recipient_matches > 1:
                    customer_display_name = (
                        f"⚠️ {_recipient_matches} people named {pl.recipient_name}"
                    )
                elif pl.recipient_name and roster_truncated:
                    # We only read part of the company — say so rather than
                    # claiming this person isn't there.
                    customer_display_name = (
                        f"⚠️ {pl.recipient_name} not in first {len(company_roster)}"
                    )
                elif pl.recipient_name:
                    customer_display_name = f"⚠️ {pl.recipient_name} not found"
                else:
                    customer_display_name = "⚠️ Recipient required"
        else:
            # Either a non-full-scope role (guest, etc.) or a full-scope
            # customer line that fell back to self-ordering above.
            customer_id = self_customer_id
            customer_display_name = "Order"
            is_self_order = True
            shipping_address = None
            billing_address = None

        # Unresolved reason — now distinguishes "not provided" from "not found"
        _customer_unresolved = customer_id is None
        _product_unresolved = pl.product_id is None

        if _is_rep and _customer_unresolved and not pl.email:
            if not company_scope:
                _customer_reason = "company_not_provided"
            elif not company_roster:
                _customer_reason = "company_not_found"
            elif pl.recipient_name and _recipient_matches > 1:
                _customer_reason = "recipient_ambiguous"
            elif pl.recipient_name:
                _customer_reason = "recipient_not_found"
            else:
                _customer_reason = "recipient_required"
        else:
            _customer_reason = "email_not_provided" if (_is_rep and not pl.email) else "email_not_found"

        if _product_unresolved and _customer_unresolved:
            unresolved = True
            unresolved_reason = "both_not_found"
        elif _product_unresolved:
            unresolved = True
            unresolved_reason = "product_not_found"
        elif _customer_unresolved:
            unresolved = True
            unresolved_reason = _customer_reason
        else:
            unresolved = False
            unresolved_reason = None

        result_lines.append(BulkOrderLine(
            raw_fragment=pl.raw_fragment,
            company_name=pl.company_name,
            recipient_name=pl.recipient_name,
            email=pl.email,
            product_name=pl.product_name,
            quantity=pl.quantity,
            quantity_explicitly_set=pl.quantity_explicitly_set,
            product_id=pl.product_id,
            variation_id=pl.variation_id,
            customer_id=customer_id,
            customer_display_name=customer_display_name,
            is_self_order=is_self_order,
            shipping_address=shipping_address,
            billing_address=billing_address,
            is_reorder=pl.is_reorder,
            reorder_source_order_id=pl.reorder_source_order_id,
            unresolved=unresolved,
            unresolved_reason=unresolved_reason,
            unmatched_variant_hint=pl.unmatched_variant_hint,
            blank_variant_axes=list(pl.blank_variant_axes or []),
            candidate_variation_ids=list(pl.candidate_variation_ids or []),
            specified_variant_axes=list(pl.specified_variant_axes or []),
            self_contained_variant=pl.self_contained_variant,
            variant_meta=dict(getattr(pl, "variant_meta", None) or {}),
        ))

    logger.info(
        f"bulk_parser | parsed {len(result_lines)} lines | "
        f"unresolved={sum(1 for l in result_lines if l.unresolved)}"
    )
    return result_lines