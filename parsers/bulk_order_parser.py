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
    # Terms the rep typed that no VARIATION enumerates — typically because
    # that axis is "Any" on the variations and its real options live on the
    # parent product. Carried through so the variant prompt can pre-select
    # them instead of asking for a value the rep already supplied.
    unmatched_variant_terms: list = field(default_factory=list)
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


# ── Optional checkout-field clauses ("rep X", "order type Y", ...) ──────────
# Keyword-introduced clauses that may appear ANYWHERE in a bulk order message
# and in any order:
#
#   Order 1 each allspice, adams for Andrew Gazda at Gensler,
#           rep John Smith, order type new deal
#
# They MUST be stripped before Step 1 splits on commas. That split turns every
# comma-separated chunk into an order fragment, so ", rep John Smith" would
# otherwise be resolved as a product line. Removing the clauses first is also
# exactly what makes them position-independent: what remains is the plain
# product/recipient text the existing parser already handles.
#
# Each clause runs to the next comma or end of string. Comma-only, not
# "next keyword", deliberately: order-type labels are multi-word ("New Deal",
# "Presentation/Library") and a project name may legitimately contain a word
# that also opens a clause.
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
# "project Midtown Office") — used ONLY for typo-guard token extraction, not
# for parsing (that's _FIELD_CLAUSE_RE below). Same single-source-of-truth
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

_FIELD_CLAUSE_RE = re.compile(
    r"(?:^|,)\s*(?P<kw>"
    + "|".join(_FIELD_CLAUSE_OPENERS)
    # A clause value ends at a comma or end of string — but ALSO at a bare
    # " for "/" at ", because those are the order's own company/recipient
    # markers. Without this, "order type new deal, rep John Smith for Gensler"
    # captures "John Smith for Gensler" as the rep name AND silently eats the
    # company tail, leaving the order unscoped. Same silent-drop shape as the
    # _FOR_COMPANY_RE over-capture bug.
    + r")\s+(?P<val>[^,]+?)(?=\s*,|\s*$|\s+(?:for|at)\s+)",
    re.I,
)


def _slot_for_keyword(kw: str) -> str:
    norm = re.sub(r"\s+", " ", kw.strip().lower())
    for slot, patterns in _FIELD_CLAUSE_KEYWORDS.items():
        for pat in patterns:
            if re.fullmatch(pat, norm, re.I):
                return slot
    return ""


def _extract_field_clauses(text: str):
    """Strip optional checkout-field clauses out of `text`.

    Returns (remaining_text, values, notices) where `values` maps the address
    block's own field keys (project_rep / billing_field_type) to resolved
    values, and `notices` carries a human-readable line per clause that could
    NOT be resolved.

    Nothing here guesses. An unrecognised or ambiguous value is left UNSET and
    reported — which lands it on the address card's existing missing-field
    prompt, the same review step a blank field already reaches, rather than
    writing something the storefront cannot render.
    """
    values, notices = {}, []
    if not text:
        return text, values, notices

    def _replace(m):
        slot = _slot_for_keyword(m.group("kw"))
        raw = (m.group("val") or "").strip(" .;:")
        if not slot or not raw:
            return m.group(0)          # not ours — leave the text alone

        if slot == "project_rep":
            resolved, note = _resolve_rep_clause(raw)
        elif slot == "billing_field_type":
            resolved, note = _resolve_order_type_clause(raw)
        else:
            resolved, note = _resolve_project_clause(raw)

        if resolved:
            values[slot] = resolved
        if note:
            notices.append(note)
        # Consume the clause either way: it was clearly a field clause, not an
        # order line, so leaving it in would make the parser hunt for a product
        # called "John Smith".
        return "," if m.group(0).lstrip().startswith(",") else ""

    remaining = _FIELD_CLAUSE_RE.sub(_replace, text)
    remaining = re.sub(r",\s*,", ",", remaining)
    remaining = remaining.strip().strip(",").strip()
    return remaining, values, notices


def _resolve_rep_clause(raw: str):
    """Validate a typed rep name against the project_rep dropdown options.

    Stores the rep's EMAIL, which is what `_billing_project_rep` holds and
    what the widget's <select> matches on — the label is only what a human
    types. Unknown or ambiguous returns no value plus a notice.
    """
    try:
        from utils.checkout_fields import rep_directory
        directory = rep_directory()
    except Exception:
        directory = {}

    if not directory:
        # Options unreachable — fail OPEN rather than blocking the order, same
        # contract as is_known_rep(). Nothing is stored: an unvalidated email
        # guess here would be exactly the blank-<select> bug.
        logger.warning(f"[FieldClause] rep options unavailable — '{raw}' not applied")
        return "", (
            f"⚠️ Couldn't check the rep list right now, so **{raw}** wasn't "
            f"applied. You can set the rep on the review step below."
        )

    needle = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    exact, partial = [], []
    for email, name in directory.items():
        n = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if not n:
            continue
        if n == needle:
            exact.append((name, email))
        elif needle and (n.startswith(needle) or needle in n):
            partial.append((name, email))

    hits = exact or partial
    if len(hits) == 1:
        name, email = hits[0]
        logger.info(f"[FieldClause] rep '{raw}' → {name} <{email}>")
        return email, ""
    if len(hits) > 1:
        names = ", ".join(sorted(n for n, _ in hits))
        return "", (
            f"⚠️ **{raw}** matches more than one rep ({names}). "
            f"Pick the right one on the review step below."
        )
    return "", (
        f"⚠️ **{raw}** isn't one of the reps on file. "
        f"Choose a rep on the review step below."
    )


def _resolve_project_clause(raw: str):
    """Pass a typed project name straight through.

    No validation, deliberately: billing_project is a plain <input> on the
    checkout form, not a <select>, so there is no option list to check against
    and whatever the user types IS the value. The other two clause slots are
    selects, where an unrecognised value must be left unset or it renders
    blank in the widget while still satisfying the required-field gate.
    """
    cleaned = str(raw or "").strip(" .;:,-")
    if not cleaned:
        return "", ""
    logger.info(f"[FieldClause] project → {cleaned!r}")
    return cleaned, ""


def _resolve_order_type_clause(raw: str):
    """Validate a typed order type and translate label → stored value."""
    try:
        from utils.checkout_fields import match_order_type
        res = match_order_type(raw)
    except Exception as exc:
        logger.warning(f"[FieldClause] order type check failed: {exc}")
        return "", (
            f"⚠️ Couldn't check the order types right now, so **{raw}** "
            f"wasn't applied. You can set it on the review step below."
        )

    status = res.get("status")
    if status == "matched":
        logger.info(f"[FieldClause] order type '{raw}' → {res['value']} ({res['label']})")
        return res["value"], ""
    if status == "unvalidated":
        logger.warning(f"[FieldClause] order type '{raw}' stored unvalidated")
        return res["value"], ""
    if status == "ambiguous":
        opts = ", ".join(res.get("candidates") or [])
        return "", (
            f"⚠️ **{raw}** matches more than one order type ({opts}). "
            f"Pick one on the review step below."
        )
    opts = ", ".join(res.get("candidates") or [])
    return "", (
        f"⚠️ **{raw}** isn't a valid order type"
        + (f". Valid options: {opts}." if opts else ".")
        + " Set it on the review step below."
    )


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

    # ── Step -0.5: Optional checkout-field clauses ("rep X", "order type Y") ─
    # Runs BEFORE company scope and before the comma split. Company extraction
    # scans trailing "for"/"at" tails and the split turns commas into order
    # fragments, so a trailing ", rep John Smith" would otherwise be read as
    # either a company tail or a product line. Stripping the clauses first is
    # what makes them order-independent.
    text, _field_clause_values, _field_clause_notices = _extract_field_clauses(text)
    if _field_clause_values or _field_clause_notices:
        logger.info(
            f"bulk_parser | field clauses | values={_field_clause_values} "
            f"| notices={len(_field_clause_notices)}"
        )
    if meta_out is not None:
        # The handler stamps these onto the address block and surfaces the
        # notices on the address card. Always set (even when empty) so the
        # handler can distinguish "parsed, nothing given" from "key absent".
        meta_out["field_clause_values"] = dict(_field_clause_values)
        meta_out["field_clause_notices"] = list(_field_clause_notices)

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
        if _is_dimension or any(_key == o or _key in o for o in _opts):
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

        # Haystacks must be normalised the SAME WAY as the needle. They were
        # only .lower()'d before, which failed on this store's real data:
        # Andrew_Gazda@gensler.com gives an email local part of
        # "andrew_gazda", and the needle "Andrew Gazda" normalises to
        # "andrew gazda" — the underscore alone defeated both passes below,
        # since a multi-word needle can never be an element of h.split()
        # either. Same class of failure for any separator (dots in
        # first.last@, hyphens in double-barrelled surnames).
        _norm = lambda s: re.sub(r'[^a-z0-9]+', ' ', str(s or "").lower()).strip()

        matches = []
        for c in company_roster:
            first = _norm(c.get("first_name", ""))
            last = _norm(c.get("last_name", ""))
            full = f"{first} {last}".strip()
            email_local = _norm(str(c.get("email", "") or "").split("@")[0])
            # Billing name as an ADDITIONAL haystack, never a replacement:
            # some accounts carry a degenerate account name (this store has
            # "Gazda Gazda") where billing holds the real one. The existing
            # repair for that lives in _roster_entry_to_resolution(), which
            # runs for DISPLAY only and AFTER matching — so it never helped
            # the lookup itself. Added here rather than moved, because
            # billing is NOT a better source in general (eleanor_baker@
            # bills as "Jennifer McKinney"); matching on either is right,
            # preferring billing outright is not.
            _bill = c.get("billing", {}) or {}
            bill_first = _norm(_bill.get("first_name", ""))
            bill_last = _norm(_bill.get("last_name", ""))
            bill_full = f"{bill_first} {bill_last}".strip()
            haystacks = {
                h for h in (
                    first, last, full, email_local,
                    bill_first, bill_last, bill_full,
                ) if h
            }
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
        #
        # ALSO gated on line count: the self-order cart route is only taken
        # for a SINGLE line. Two or more lines is a bulk order, so a customer
        # gets the same company -> recipient -> address flow a rep gets, even
        # when they named nobody. Without this the lines resolve to the
        # customer themselves, `unresolved_reason` is None, and every asking
        # gate downstream (Step 4.55 company, 4.56 recipient, 4.57 address)
        # finds an empty list and silently falls through to the cart — which
        # is why a rep could complete this order and a customer could not.
        # Counted in LINES, not units: 5x of one product is still one line.
        _customer_self_fallback = (
            _is_rep and not _is_true_rep
            and len(pre_lines) <= 1
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
            unmatched_variant_terms=list(pl.unmatched_variant_terms or []),
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