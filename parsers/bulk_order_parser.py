"""
parsers/bulk_order_parser.py — Parse free-text bulk order utterances into
resolved BulkOrderLine objects.

No Flask. WooCommerce API calls only (via woo_client / endpoints).

Public API:
    parse_bulk_order_utterance(text, store_loader, self_customer_id)
        → List[BulkOrderLine]
"""

import re
import difflib
from dataclasses import dataclass, field
from typing import Optional, List

from woo_client import woo_client
from ecommerce import endpoints
from chat_logger import get_logger
from platform_config import current_backend
from handlers.chat_utils import normalize_spelling_variants, _attribute_display_name, variation_declares_self_contained_term, _normalize_term_key
from models import ExtractedEntities
from classifier.extractors import extract_attributes
from api_builder.store_helpers import resolve_attr_filters
from api_builder.filter_builder import build_advanced_filter_call

logger = get_logger("miraq_chat")

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
    # Terms the rep typed that no VARIATION enumerates — typically because
    # that axis is "Any" on the variations and its real options live on the
    # parent product. Carried through so the variant prompt can pre-select
    # them instead of asking for a value the rep already supplied.
    unmatched_variant_terms: list = field(default_factory=list)
    # EVERY term the rep typed for this line, matched or not. Carried out of
    # the parser because unmatched_variant_terms alone is the wrong basis for
    # deciding what still needs asking: it holds only terms NO variation
    # enumerates, whereas whether a term is already answered depends on which
    # axes the CHOSEN variation leaves "Any".
    variant_terms: list = field(default_factory=list)
    # Terms each valid on their own that NO single variation carries
    # together (Allspice has no Beleza + Honed). Separate from
    # unmatched_variant_terms because those are PRE-SELECTED, and
    # pre-selecting one of these would offer an unorderable pairing.
    conflicting_variant_terms: list = field(default_factory=list)
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
    # The hint split into individual attribute terms, e.g. "Beleza, Honed,
    # 5\"x10\"" → ["Beleza", "Honed", "5\"x10\""]. A variation must satisfy
    # EVERY term to match (see Step 3.5), which is what lets one product carry
    # colour + finish + size at once.
    #
    # Deliberately ADDITIVE rather than replacing variant_hint: the hint
    # string is still what the single-term path, the shared-hint propagation
    # in Step 3.7 and the unmatched-hint message all read, so existing shapes
    # keep their exact behaviour. Empty here means "single-term line" and the
    # original matching runs unchanged.
    variant_terms: list = field(default_factory=list)
    # A descriptor typed BEFORE the product name ("Order 1 Chip Card each
    # Allspice Beleza..."). Held separately from variant_hint because it
    # usually applies to the WHOLE order, not just the line it touches —
    # Step 3.75 propagates it. Kept apart so it can never overwrite the line's
    # own attribute.
    leading_hint: str = ""
    # True only when THIS fragment's own text carried a count. Distinct from
    # quantity_explicitly_set, which is also set when a shared count is
    # distributed across every line by the "each" rule (Step 1.6) or the
    # leading-quantity rule (Step 2.5).
    #
    # Attribute absorption needs the narrow question: "and 3 Adams" is a new
    # order line, but a 1 propagated onto every line from the front of the
    # message is not evidence of anything. Reading the broad flag meant that
    # after Step 2.5 ran, EVERY line looked explicitly quantified and no
    # attribute fragment could ever be absorbed.
    quantity_self_declared: bool = False
    # Terms that matched no option on this product — reported individually so
    # a three-term hint can say WHICH term failed instead of rejecting the
    # whole string, which would repeat the silent/opaque-drop pattern one
    # level down.
    unmatched_variant_terms: list = field(default_factory=list)
    # Terms each valid on their own that NO single variation carries
    # together (Allspice has no Beleza + Honed). Separate from
    # unmatched_variant_terms because those are PRE-SELECTED, and
    # pre-selecting one of these would offer an unorderable pairing.
    conflicting_variant_terms: list = field(default_factory=list)
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

# Text that names a company EXPLICITLY — a scope marker plus the word
# "company", or a trailing "at <name>". Deliberately excludes a bare
# "for <name>" tail: that is genuinely ambiguous between a person and a
# company, and is resolved later against the roster rather than guessed at
# classification time.
#
# routes/chat.py imports this for the typo guard (_is_inline_bulk_order).
# The parser itself no longer reads company names: a bulk order is always the
# shopper's own and goes to their cart.
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
# Any scope tail, used ONLY for typo-guard token extraction (not for parsing).
# Unanchored so it catches "for ram" mid-fragment as well as trailing tails.
COMPANY_SCOPE_TAIL_RE = re.compile(
    r'\b(?:for(?:\s+company)?|at)\s+([^,]+)', re.I
)


# ── Checkout-field clause words ("rep X", "order type Y", "project Z") ──────
# The parser no longer reads these clauses. The keyword list stays only for
# routes/chat.py's typo guard (FIELD_CLAUSE_SCOPE_TAIL_RE /
# FIELD_CLAUSE_MARKER_WORDS below), which keeps the corrector from rewriting
# the words and the text after them.
_FIELD_CLAUSE_KEYWORDS = {
    # canonical slot  ->  accepted openers, longest first so "order type"
    #                     wins over a bare "type"
    "project_rep":        [r"rep", r"sales\s+rep", r"cs\s+rep"],
    "billing_field_type": [r"order\s+type", r"field\s+type", r"deal\s+type"],
    # Free text — billing_project is a plain <input> on the checkout form, not
    # a <select>, so there is no option list to validate against and whatever
    # the user types IS the value. Contrast project_rep / billing_field_type,
    # which are selects and must resolve to a real option or be left unset.
    "billing_project":    [r"project"],
}

# Tile/sample dimensions: 5"x10", 12" x 12", 5x10, 12 X 24, 5'x10'.
# Matched so the quantity scanner can remove them before looking for a count —
# every one of these starts with a digit and would otherwise be read as "order
# N of something". Also covers a lone measurement ('12"') for the same reason.
_DIMENSION_RE = re.compile(
    r'\b\d+\s*(?:["\u201d\u2033\'\u2032]|in\.?|inch(?:es)?)?\s*[x\u00d7]\s*'
    r'\d+\s*(?:["\u201d\u2033\'\u2032]|in\.?|inch(?:es)?)?'
    r'|\b\d+\s*(?:["\u201d\u2033]|inch(?:es)?)',
    re.I,
)

_FIELD_CLAUSE_OPENERS = sorted(
    (kw for kws in _FIELD_CLAUSE_KEYWORDS.values() for kw in kws),
    key=len,
    reverse=True,
)

# Any FIELD-CLAUSE scope tail ("rep John Smith", "order type new deal",
# "project Midtown Office") — used ONLY for typo-guard token extraction.
# Same single-source-of-truth
# contract as COMPANY_SCOPE_TAIL_RE: routes/chat.py imports this and the
# marker words, so a keyword added to _FIELD_CLAUSE_KEYWORDS above is
# protected from typo correction automatically, with no second edit in
# chat.py. Without it, "project Midtown Office" was corrected to "product
# Midtown onice" — the same class of bug COMPANY_SCOPE_TAIL_RE guards against
# for "at Beck" → "at back", one keyword over.
FIELD_CLAUSE_SCOPE_TAIL_RE = re.compile(
    r"\b(?:" + "|".join(_FIELD_CLAUSE_OPENERS) + r")\s+([^,]+)",
    re.I,
)

# The bare opener words themselves ("rep", "project", "type", ...) — these
# must never be rewritten either, independent of whatever value follows.
#
# Derived from the same source, but split on ANY whitespace pattern rather
# than the literal "\s+" substring: keying on one exact spelling meant that
# writing a future keyword as r"order\s*type" or r"order type" would silently
# yield one junk token instead of two, with no error anywhere.
FIELD_CLAUSE_MARKER_WORDS = frozenset(
    word
    for kws in _FIELD_CLAUSE_KEYWORDS.values()
    for kw in kws
    for word in re.split(r"\\s[+*]|\s+", kw)
    if word and word.isalpha()
)


def parse_bulk_order_utterance(
    text: str,
    store_loader,
    self_customer_id: Optional[str] = None,
    meta_out: Optional[dict] = None,
) -> List[BulkOrderLine]:
    """
    Parse a free-text bulk order utterance into a list of resolved BulkOrderLine objects.

    Steps:
      1. Split into fragments (comma / " and <digit>")
      2. Per-fragment extraction (qty, company, product, reorder flag)
      3. Product resolution (store_loader, no API call)
      4. Assemble final BulkOrderLine objects

    Every line is the shopper's own order (it goes to their cart), so nothing
    here looks up companies, recipients or other customers.
    """
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

    # Pass 3: PRODUCT-ANCHORED split ────────────────────────────────────────
    # Passes 1-2 only break on commas and "and", which is not enough once a
    # product can carry its own comma-separated attribute list. Concretely,
    # 'Allspice Beleza, Honed, 5"x10" and Adams Beige' leaves the fragment
    # '5"x10" and Adams Beige' intact, because the "and" split needs a catalog
    # name on BOTH sides and the left side here is a size. Step 2 then resolves
    # that fragment to Adams and reads '5"x10" and' as ADAMS's variant hint —
    # so Allspice silently loses its size and Adams silently gains one. Wrong
    # answer, no error: the failure mode this codebase keeps getting bitten by.
    #
    # So: inside a fragment, a catalog product name that does not start the
    # fragment begins a NEW fragment, and whatever preceded it belongs to the
    # product before it (Diya's rule — attributes attach to the product
    # immediately before them).
    #
    # Gated on "some earlier fragment already named a product". Without that
    # gate this would also split the FIRST fragment's leading hint away:
    # "Order 1 Chip Card each Allspice Beleza" would become ["Order 1 Chip
    # Card each", "Allspice Beleza"] and the shared Chip Card hint would be
    # lost. Leading text before the first product is a hint; leading text
    # after one is a trailing attribute of that earlier product.
    _anchored = []
    _seen_product = False
    for frag in final_fragments:
        _has_name = any(n in frag.lower() for n in _catalog_names)
        if not _seen_product or not _has_name:
            _anchored.append(frag)
            _seen_product = _seen_product or _has_name
            continue

        # Earliest product-name occurrence, longest name first so "Adams
        # Mosaic" is preferred over "Adams" at the same position.
        _cut = None
        for _p in _products_by_name_len:
            _m = re.search(r'\b' + re.escape(_p["name"]) + r'\b', frag, re.I)
            if _m and _m.start() > 0 and (_cut is None or _m.start() < _cut):
                _cut = _m.start()
        if _cut is None:
            _anchored.append(frag)
            _seen_product = True
            continue

        _before = frag[:_cut].strip(" ,.-")
        # Text before the product name is only a NEW-LINE marker, not an
        # attribute, when it is just a quantity and/or a connector: "3 Adams
        # Beige" and "and Tara Cloud" must stay whole. Splitting them stranded
        # the "3" as its own fragment (so Adams silently lost its quantity)
        # and left a bare "and" that Step 2 reported as an unknown product.
        _before_core = re.sub(
            r'\b(?:and|each|every|per|order|buy|purchase|reorder|re-order)\b',
            ' ', _before, flags=re.I,
        )
        _before_core = re.sub(
            r'\b(?:\d+|' + '|'.join(_WORD_NUMERALS) + r')\b',
            ' ', _before_core, flags=re.I,
        ).strip(" ,.-")

        if not _before_core:
            # Keep the fragment whole, minus any leading connector so the
            # product-name matcher and the leading-hint logic see clean text.
            _anchored.append(re.sub(r'^\s*and\s+', '', frag, flags=re.I).strip())
            _seen_product = True
            continue

        # _before_core is ONLY an emptiness probe — it has had digits stripped
        # out, so emitting it would mangle a size ('5"x10"' → '"x10"'). The
        # fragment that goes out is the original text, minus a leading or
        # trailing connector.
        _before_clean = re.sub(
            r'^\s*and\s+|\s+and\s*$', '', _before, flags=re.I
        ).strip(" ,.-")
        _anchored.append(_before_clean or _before)
        _anchored.append(frag[_cut:].strip())
        _seen_product = True

    final_fragments = _anchored

    # ── Step 1.6: Transaction-wide "each" quantity ─────────────────────────
    each_quantity = None
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

        # A DIMENSION is not a count. '5"x10"' begins with a digit, so the
        # bare \d+ scan below read it as quantity 5 and left '"x10"' behind,
        # which then failed to resolve as a product ('unresolved product
        # "x10"'). It also set quantity_explicitly_set, which suppressed the
        # attribute-absorption step — so the size was lost twice over.
        #
        # Stripped before the scan rather than pattern-matched around, so a
        # real count sitting next to a size still works: "Order 3 12x12 Adams"
        # loses the 12x12 here and correctly finds 3.
        qty_scan_text = _DIMENSION_RE.sub(' ', qty_scan_text)

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
            company_name="",
            recipient_name="",
            email="",
            # Captured from THIS fragment's own text, before any shared-count
            # distribution below can overwrite quantity_explicitly_set.
            quantity_self_declared=(qty_match is not None),
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

    # ── Variation lookup helpers (shared) ────────────────────────────────────
    # Declared BEFORE Step 3 because three separate steps need the same
    # variation data and must not fetch it three times: Step 3c's guard
    # against fuzzy-matching an attribute word to a product name, Step 3.4's
    # attribute absorption, and Step 3.5's variation matching.
    _variant_cache: dict = {}   # product_id → list[variation dicts]
    _variant_fetch_failed: set = set()   # product_ids whose lookup errored out

    def _variations_for(product_id):
        """Fetch-and-cache variations. None means 'could not determine'."""
        if product_id in _variant_fetch_failed:
            return None
        if product_id not in _variant_cache:
            data = endpoints.list_variants_resolved(
                product_id=product_id,
                store_loader=store_loader,
                per_page=100,
            )
            if data is None:
                _variant_fetch_failed.add(product_id)
                return None
            _variant_cache[product_id] = data
        return _variant_cache[product_id]

    def _option_values(product_id):
        """Every option string across every variation of a product."""
        out = set()
        for var in (_variations_for(product_id) or []):
            _al = var.get("attributes", [])
            _opts = (
                list(_al.values()) if isinstance(_al, dict)
                else [a.get("option", "") for a in _al if isinstance(a, dict)]
            )
            for o in _opts:
                if str(o).strip():
                    out.add(_normalize_term_key(normalize_spelling_variants(str(o))))
        return out

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
            # An ATTRIBUTE word must never become a product here. At cutoff
            # 0.6 "Matte" reaches "Hattie" and "Honed" reaches "Monet" — both
            # real catalog products, neither anywhere in the message. That
            # produced phantom order lines AND stole the sizes that followed:
            # '12"x12"' was absorbed into "Hattie" because Hattie had, by
            # then, become the nearest preceding product.
            #
            # Only fragments that could plausibly be an attribute of the
            # product before them are protected, and only when that product's
            # own options confirm it — so a genuine typo'd product name
            # ("Allspce") still fuzzy-matches as before. Checked before the
            # fuzzy call rather than after, because once product_id is set the
            # attribute is indistinguishable from a real match.
            _skip_fuzzy = False
            _frag_key = _normalize_term_key(
                normalize_spelling_variants(pl.product_name.strip(" ,.-"))
            )

            # Guard A — the catalog's OWN typing of the term. store_loader
            # classifies every catalog term as category/tag/attribute/
            # product_word, so "matte" and "honed" are already known to be
            # attributes. Nothing consulted that here: fuzzy_protected_words
            # is read only by utils/typo_correction.py, so these words were
            # correctly shielded from the TYPO corrector (which is why they
            # arrived intact) and then mangled by this second, unrelated
            # fuzzy matcher a few steps later.
            #
            # Safe at this point specifically: exact product-name matches were
            # already taken in 3a/3b, so anything reaching 3c is not a catalog
            # product name, and a term the catalog calls an attribute is not
            # about to become a product by approximation.
            if _frag_key and store_loader is not None:
                _types = getattr(store_loader, "fuzzy_vocab_types", None) or {}
                _raw = pl.product_name.strip(" ,.-").lower()
                if _types.get(_raw) == "attribute":
                    _skip_fuzzy = True
                    logger.info(
                        f"bulk_parser | '{pl.product_name}' is a catalog ATTRIBUTE "
                        f"term — not fuzzy-matching it to a product name"
                    )

            # Guard B — this specific product's own options. Narrower and
            # independent of how the catalog types the word, so it still
            # covers terms Guard A misses (a value indexed under a different
            # type, or one that is also a word inside some product's name).
            _prev_pid = None
            if not _skip_fuzzy:
                for _q in reversed(pre_lines[:pre_lines.index(pl)]):
                    if _q.product_id:
                        _prev_pid = _q.product_id
                        break
            if _prev_pid and _frag_key:
                for _o in _option_values(_prev_pid):
                    if _frag_key == _o or _frag_key in _o:
                        _skip_fuzzy = True
                        logger.info(
                            f"bulk_parser | '{pl.product_name}' is an option of "
                            f"product {_prev_pid} — not fuzzy-matching it to a "
                            f"product name; leaving it for attribute absorption"
                        )
                        break

            if not _skip_fuzzy:
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
                if current_backend()== "shopify":
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
                        # "for <Name>" in the remainder is not a variant
                        # hint. Bulk orders are always the shopper's own, so
                        # it is kept on the line but not used.
                        if not pl.recipient_name:
                            candidate = EMAIL_RE.sub('', _for_match.group(1)).strip().strip(', ')
                            if candidate:
                                pl.recipient_name = candidate
                    else:
                        pl.variant_hint = remainder

                # LEADING text is captured in BOTH branches, not just when the
                # remainder is empty. "Order 1 Chip Card each Allspice Beleza,
                # Honed" has a shared descriptor in front AND the product's own
                # attribute behind: reading only the remainder dropped "Chip
                # Card" on the floor with no warning. Kept in its own field so
                # it can be propagated across lines (Step 3.75) without
                # competing with the line's own hint.
                _lead_raw = pl.product_name[:_name_match.start()]
                _lead_raw = re.sub(
                    r'^\s*(?:\d+|' + '|'.join(_WORD_NUMERALS) + r')\b',
                    '', _lead_raw, flags=re.I,
                )
                _lead_raw = re.sub(r'\b(each|every|per)\b', '', _lead_raw, flags=re.I)
                _lead_raw = re.sub(
                    r'\b(order|buy|purchase|reorder|re-order)\b', '', _lead_raw, flags=re.I
                )
                _lead_raw = re.sub(r'\b(from|of|and)\s*$', '', _lead_raw, flags=re.I)
                _lead_raw = _lead_raw.strip(" ,.-")
                if _lead_raw and _lead_raw.lower() != (pl.variant_hint or "").lower():
                    pl.leading_hint = _lead_raw

                if not remainder:
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

    # ── Step 3.4: Absorb orphan ATTRIBUTE fragments into the product before ──
    # "Order 1 Allspice Beleza, Honed, 5\"x10\"" splits on commas. Only the
    # first fragment carries a product; the rest are attribute terms
    # belonging to the product immediately before them.
    #
    # Absorbed only after confirming the fragment is a real option on that
    # product (or is a dimension, which is never a product name), so a
    # genuinely unknown product still stays unresolved and reports as it
    # always did. Nothing is guessed.
    _absorbed_idx = set()
    for _i, pl in enumerate(pre_lines):
        if pl.product_id or pl.email:
            continue
        if pl.quantity_self_declared:
            # An explicit quantity in THIS fragment's own text marks a new
            # order line ("... and 3 Adams"), never a trailing attribute.
            # Deliberately NOT quantity_explicitly_set: that is also true for
            # a count distributed across every line from the front of the
            # message, which would block absorption on all of them.
            continue
        _candidate = (pl.product_name or "").strip(" ,.-")
        # An attribute fragment may carry the RECIPIENT for the product it
        # belongs to: '... Beleza, Honed, 5"x10" for Annabelle Damon, Adams
        # ...'. The size is Allspice's and so is Annabelle. Absorb the term
        # and hand the recipient to the same product, rather than skipping
        # the fragment and stranding both.
        _carried_recipient = pl.recipient_name or ""
        if not _candidate and _carried_recipient:
            continue
        if not _candidate:
            continue
        _prev = None
        for _j in range(_i - 1, -1, -1):
            if _j in _absorbed_idx:
                continue
            if pre_lines[_j].product_id:
                _prev = pre_lines[_j]
                break
        if _prev is None:
            continue
        _opts = _option_values(_prev.product_id)
        if not _opts:
            continue                      # fetch failed or no variations — leave alone
        _key = _normalize_term_key(normalize_spelling_variants(_candidate))
        if not _key:
            continue
        # A DIMENSION is never a product name, so it needs no proof from the
        # option list. It often can't get one: a variation may leave an axis
        # blank ("Any"), with the real options living on the PARENT product —
        # which is why Adams (whose 32 variations enumerate 12"x12") absorbed
        # its size while Allspice and Tara, whose variations leave Sample Size
        # blank, rejected theirs and left them as phantom order lines.
        _is_dimension = bool(_DIMENSION_RE.fullmatch(_candidate.strip()))
        # Same blank-axis problem, but for NON-dimension terms. "Polished" is a
        # finish, and Enduring 2.0 leaves Finish 'Any' on its variations, so it
        # appeared in no option list and was not a dimension either — it failed
        # both tests and survived as a phantom order line called "Polished"
        # with product_id null and "Recipient required" against it.
        #
        # The catalog already knows the word is an attribute value: Guard A in
        # Step 3c uses `fuzzy_vocab_types` for exactly this, to stop such a term
        # being fuzzy-matched into a product name. Reused here rather than
        # fetching the parent, and safe for the same reason it is safe there —
        # 3a/3b already took every exact product-name match, so a fragment that
        # reaches this point with no product_id is not a product.
        # Any non-product catalog type counts, not "attribute" alone.
        # build_fuzzy_vocab adds categories and tags BEFORE attributes and
        # skips a word already present (`word not in vocab_types`), so a value
        # that is also a category or tag name is typed as THAT — "Polished" is
        # one, which is why an == "attribute" test never fired for it and the
        # phantom line survived. product_word is excluded because a real
        # product name must keep failing this gate and stay unresolved.
        _vocab_types = getattr(store_loader, "fuzzy_vocab_types", None) or {}
        _cand_type = _vocab_types.get(_candidate.lower())
        _is_catalog_attr = bool(_cand_type) and _cand_type != "product_word"
        if _is_dimension or _is_catalog_attr or any(_key == o or _key in o for o in _opts):
            _prev.variant_terms.append(_candidate)
            if _carried_recipient and not _prev.recipient_name:
                _prev.recipient_name = _carried_recipient
                logger.info(
                    f"bulk_parser | recipient '{_carried_recipient}' carried from "
                    f"attribute fragment to product {_prev.product_id}"
                )
            _absorbed_idx.add(_i)
            logger.info(
                f"bulk_parser | absorbed attribute fragment '{_candidate}' into "
                f"product {_prev.product_id} ('{_prev.product_name}')"
            )

    if _absorbed_idx:
        pre_lines = [p for i, p in enumerate(pre_lines) if i not in _absorbed_idx]

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

    # ── Step 3.75: Shared LEADING descriptor → every line's terms ────────────
    # "Order 1 Chip Card each Allspice Beleza, Honed, 5\"x10\" and Adams Beige,
    # Matte, 12\"x12\"" — "Chip Card" sits in front of the whole list and
    # applies to every product, while each product also carries its own
    # colour/finish/size.
    #
    # Step 3.7 above cannot express this: it MOVES a single hint onto lines
    # that have none, so here (where every line already has its own hint) it
    # correctly declines to fire, and the shared descriptor was simply lost.
    # This adds it as an EXTRA term instead of replacing anything, which is
    # the only way a line can carry both.
    #
    # Conservative in the same way as 3.7 and Step 1.6: fires only when
    # exactly ONE line carries a leading descriptor and that line is the
    # FIRST — the only position where the text reads as covering the whole
    # order. A descriptor in front of the third product describes the third
    # product, and is left alone.
    _leading = [pl for pl in pre_lines if pl.product_id and pl.leading_hint]
    if len(_leading) == 1 and pre_lines and _leading[0] is pre_lines[0]:
        _shared_lead = _leading[0].leading_hint
        _applied = 0
        for pl in pre_lines:
            if not pl.product_id:
                continue
            # Seed from the line's own hint first so ordering stays as typed
            # and the shared descriptor never displaces it.
            if not pl.variant_terms and pl.variant_hint:
                pl.variant_terms = [pl.variant_hint]
            if not any(
                _normalize_term_key(t) == _normalize_term_key(_shared_lead)
                for t in pl.variant_terms
            ):
                pl.variant_terms.append(_shared_lead)
                _applied += 1
        if _applied:
            logger.info(
                f"bulk_parser | shared leading descriptor '{_shared_lead}' "
                f"applied as a term to {_applied} line(s)"
            )

    # Seed variant_terms from the line's own trailing hint so the matcher has
    # the full list in one place. Done after absorption so the product's own
    # remainder ("Beleza") leads and the absorbed ones follow, preserving the
    # order the user typed.
    #
    # Idempotent: Step 3.75 may already have seeded this line before appending
    # the shared descriptor. Inserting again would put the hint in twice —
    # harmless for the AND-match (a term matching itself twice is still one
    # constraint) but it would show up doubled in the unmatched-term message.
    for pl in pre_lines:
        if not pl.variant_hint:
            continue
        if not pl.variant_terms:
            continue                      # single-term line — original path
        if not any(
            _normalize_term_key(t) == _normalize_term_key(pl.variant_hint)
            for t in pl.variant_terms
        ):
            pl.variant_terms.insert(0, pl.variant_hint)

    # ── Step 3.5: Variation resolution (API call per unique product with a hint) ─
    # NOTE: _variant_cache / _variant_fetch_failed are declared in Step 3.4
    # above, which already populated them for any product it inspected.
    # Re-declaring them here would discard that work and re-fetch.

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

        def _var_options(var):
            _al = var.get("attributes", [])
            return (
                list(_al.values()) if isinstance(_al, dict)
                else [a.get("option", "") for a in _al if isinstance(a, dict)]
            )

        def _var_attr_map(var):
            """Variation attributes as axis -> value, keeping blanks.

            _var_options() flattens to a bare value list, which discards which
            axis a value belongs to AND makes a blank ("Any") axis look like a
            value that simply matches nothing. Both matter when deciding
            whether a variation can serve a term.
            """
            _al = var.get("attributes", [])
            if isinstance(_al, dict):
                return {str(k): (v or "") for k, v in _al.items()}
            _out = {}
            for a in _al:
                if not isinstance(a, dict):
                    continue
                _key = a.get("name") or a.get("slug") or a.get("id")
                if _key is not None:
                    _out[str(_key)] = a.get("option", "") or ""
            return _out

        def _term_hits(term, options):
            """Does `term` match ANY option on this variation?

            Same two-tier comparison the single-hint path has always used:
            spelling-normalised substring first (this catalog mixes US/UK
            spellings), then a whitespace/hyphen-insensitive key so "chipcard"
            reaches "Chip Card".
            """
            _t = normalize_spelling_variants(term)
            if any(_t in normalize_spelling_variants(o) for o in options):
                return True
            _tk = _normalize_term_key(_t)
            return bool(_tk) and any(
                _tk in _normalize_term_key(o) for o in options if str(o).strip()
            )

        if pl.variant_terms:
            # A term that NO variation enumerates cannot narrow anything —
            # typically because that axis is blank ("Any") on the variations,
            # with the real options on the parent product. Constraining on it
            # would return zero matches and fail the whole line over a term
            # the rep supplied correctly.
            #
            # So: narrow using the terms that CAN narrow, and record the rest
            # rather than dropping them. They still reach the user through
            # unmatched_variant_terms, so a genuinely wrong term is reported
            # by name instead of silently ignored — it just no longer takes
            # the valid terms down with it.
            _all_opts = [o for var in _variant_cache[pl.product_id]
                         for o in _var_options(var)]
            pl.unmatched_variant_terms = [
                t for t in pl.variant_terms if not _term_hits(t, _all_opts)
            ]
            _effective = [t for t in pl.variant_terms
                          if t not in pl.unmatched_variant_terms]
            if pl.unmatched_variant_terms:
                logger.info(
                    f"bulk_parser | terms {pl.unmatched_variant_terms} match no "
                    f"variation of product {pl.product_id} (axis likely 'Any' on "
                    f"the parent) — narrowing on {_effective or 'nothing'} instead"
                )

            # A blank axis on a variation is WooCommerce "Any" — that variation
            # serves every option on that axis, so it must satisfy any term for
            # it. Matching a flat list of the variation's values got this wrong:
            # Pembroke's Cool Mix variation is Any on Finish and Sample Size, so
            # requiring 'Matte' and '12\"x12\"' to literally appear in its own
            # values excluded it, and a combination the store really does sell
            # was reported as "no variation has these together".
            #
            # Terms are mapped to an axis using the values other variations DO
            # enumerate; a term matching several axes stays satisfiable by any
            # of them, so the pa_colors/pa_chip-size ambiguity cannot wrongly
            # disqualify a variation here.
            _axis_values = {}
            for _v in _variant_cache[pl.product_id]:
                for _ax, _val in _var_attr_map(_v).items():
                    if str(_val).strip():
                        _axis_values.setdefault(_ax, []).append(_val)

            def _axes_for_term(term):
                return [ax for ax, vals in _axis_values.items()
                        if _term_hits(term, vals)]

            def _var_satisfies(var, term):
                _axes = _axes_for_term(term)
                _map = _var_attr_map(var)
                if not _axes:
                    return _term_hits(term, _var_options(var))
                for _ax in _axes:
                    _val = _map.get(_ax, "")
                    if not str(_val).strip():
                        return True          # "Any" — serves every option
                    if _term_hits(term, [_val]):
                        return True
                return False

            _matches = [
                var for var in _variant_cache[pl.product_id]
                if all(_var_satisfies(var, t) for t in _effective)
            ] if _effective else []

            # Every term is individually valid, yet NO variation carries them
            # together — Allspice sells Beleza in Polished and Silky, but there
            # is no Beleza + Honed.
            #
            # Do NOT pick a winner. "Beleza, Honed" is genuinely ambiguous:
            # the rep may want Beleza in another finish, or Honed in another
            # colour, and only they know which. An earlier version kept the
            # FIRST term purely because it was typed first, narrowed to its
            # variations, and announced that the other one was unavailable —
            # so a rep who wanted Honed in Pure White had Pure White thrown
            # away before they ever saw the picker.
            #
            # Instead: settle NOTHING. Every variation stays a candidate, so
            # the picker opens with the full option list and the UI's own
            # combination filtering guides them whichever way they choose
            # first. Both terms are recorded so the message can name them and
            # say why it is asking again.
            #
            # Kept out of unmatched_variant_terms deliberately: those get
            # PRE-SELECTED, which would re-introduce exactly the silent choice
            # this branch exists to avoid.
            if _effective and not _matches:
                pl.conflicting_variant_terms = list(_effective)
                _matches = list(_variant_cache[pl.product_id])
                logger.warning(
                    f"bulk_parser | product {pl.product_id}: no variation has "
                    f"{_effective} together — settling nothing and offering the "
                    f"full picker so the rep chooses which one to keep"
                )
        else:
            _matches = []
            for var in _variant_cache[pl.product_id]:
                _options = _var_options(var)
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
            # The self-contained term may be one of SEVERAL terms rather than
            # the whole hint: "Chip Card Allspice Beleza, Honed, 5\"x10\"" puts
            # Beleza in variant_hint and Chip Card in variant_terms (via the
            # shared leading descriptor). Keying only on variant_hint meant
            # the AND-match failed on "Chip Card" and this fallback then
            # declined to fire — so a combination that used to work as a bare
            # "chip card" order broke the moment attributes were added.
            _self_norm = ""
            if not _match and pl.variant_terms:
                for _axis_c, _term_norm_c, _term_name_c in _wanted:
                    if _term_norm_c and any(
                        _normalize_term_key(t) == _term_norm_c for t in pl.variant_terms
                    ):
                        _match = (_axis_c, _term_name_c)
                        _self_norm = _term_norm_c
                        break
            _pool = _variant_cache.get(pl.product_id) or []

            if _match and _pool:
                _axis, _term_name = _match
                # Narrow by the terms that are NOT the self-contained one.
                # The rep asked for a chip card OF a particular colour/finish/
                # size, so pin the variation carrying those rather than
                # _pool[0] — the arbitrary pick is only right when nothing
                # else was specified.
                _other_terms = [
                    t for t in (pl.variant_terms or [])
                    if _normalize_term_key(t) != (_self_norm or _hint_norm)
                ]
                _first = _pool[0]
                if _other_terms:
                    _narrowed = [
                        v for v in _pool
                        if all(_term_hits(t, _var_options(v)) for t in _other_terms)
                    ]
                    if _narrowed:
                        _first = _narrowed[0]
                        logger.info(
                            f"bulk_parser | chip card narrowed by {_other_terms} → "
                            f"variation {_first.get('id')}"
                        )
                    else:
                        # Those attributes exist on the product but not in
                        # combination. Say so instead of silently pinning an
                        # unrelated variation and shipping the wrong card.
                        pl.unmatched_variant_terms = list(_other_terms)
                        logger.warning(
                            f"bulk_parser | chip card requested with "
                            f"{_other_terms}, which match no single variation "
                            f"of product {pl.product_id}"
                        )
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

    # ── Step 6: Assemble final BulkOrderLine objects ──────────────────────────
    result_lines: List[BulkOrderLine] = []

    for pl in pre_lines:
        # Every line is the shopper's own order.
        customer_id = self_customer_id
        customer_display_name = "Order"
        is_self_order = True
        shipping_address = None
        billing_address = None

        _customer_unresolved = customer_id is None
        _product_unresolved = pl.product_id is None
        _customer_reason = "email_not_found"

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
            unmatched_variant_terms=list(pl.unmatched_variant_terms or []),
            variant_terms=list(pl.variant_terms or []),
            conflicting_variant_terms=list(pl.conflicting_variant_terms or []),
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