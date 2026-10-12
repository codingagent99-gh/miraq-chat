"""
api_builder/shopify_search_diag.py — evidence for "MiraQ returns N, Shopify shows M".

Opt-in, read-only, never changes a result. Enable with

    SHOPIFY_SEARCH_DIAG=1        (process env / PM2 env; off when unset)

and every Shopify product search (ShopifyGraphQLExecutor.execute_from_body)
then logs, to the normal chat log, under the prefix "[ShopifyDiag]":

  1. One line per Layer-1 candidate product with the facts that decide
     whether Shopify itself would show it, fetched LIVE from the Admin API for
     that exact product rather than inferred:
         status            ACTIVE / DRAFT / ARCHIVED
         online_store      onlineStoreUrl set = published to the Online Store
                           sales channel (null = hidden on the storefront)
         published_at      publishedAt
         total_inventory   / tracks_inventory, and how many variants are
                           availableForSale (exposes product-level vs
                           variant-level stock disagreement)
         per tag filter    the product's RAW tag spelling that MiraQ matched,
                           and whether Shopify's OWN tag search for the
                           store's reference spelling of that tag returns
                           this product
         per condition     pass/fail of each top-level filter condition, so a
                           dropped product says which condition dropped it
  2. One summary line: how many MiraQ kept, and how many of those are
     ACTIVE, on the Online Store, and agreed by Shopify's tag search — plus
     the names in each disagreeing group.

Shopify's tag search is the reference for tag matching because it is what the
admin product list and storefront tag filters are built on. MiraQ matches tags
after normalising both sides (see _norm_label in shopify_graphql_executor), so
"interior", "Interior" and "#interior" are one tag to MiraQ. Filter values are
slugs ("white-tones"), which Shopify's search would not match against a raw
"White Tones", so the search is run with the raw spellings found on the
candidates instead. The REFERENCE spelling for a filter value is the most
common one among the candidates, and it is printed in the summary; a kept
product that Shopify's search for that spelling does not return is
"tag_disagree". Every other spelling is searched too and its hit count logged,
so a case-only difference ('Interior' vs 'interior') shows up as identical or
different counts rather than being assumed either way.

Cost when enabled: one `nodes` query per 100 candidates plus one paged
id-only tag search per distinct raw tag spelling, run synchronously after the
search. Leave it off in normal operation.
"""

from __future__ import annotations

import os
from typing import Dict, List, Set, Tuple

from chat_logger import get_logger

logger = get_logger("miraq_chat")

_ENV_FLAG = "SHOPIFY_SEARCH_DIAG"
_NODES_BATCH = 100        # Admin API nodes(ids:) accepts up to 250
_TAG_SEARCH_CAP = 2000    # ids per tag spelling; beyond this the result is marked truncated

_FACTS_GQL_FULL = """
query ($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product {
      id title status tags publishedAt onlineStoreUrl totalInventory tracksInventory
    }
  }
}
"""
# Fallback if the full field set is rejected (scope or API-version change):
# still enough to test status and tags.
_FACTS_GQL_MIN = """
query ($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product { id title status tags }
  }
}
"""

_TAG_IDS_GQL = """
query ($query: String!, $first: Int!, $after: String) {
  products(first: $first, after: $after, query: $query) {
    pageInfo { hasNextPage endCursor }
    edges { node { id } }
  }
}
"""


def enabled() -> bool:
    return os.getenv(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


# ── Shopify lookups ──────────────────────────────────────────────────────────

def _fetch_facts(gids: List[str], token: str, domain: str, gql) -> Tuple[Dict[str, dict], str]:
    """gid → live product facts. Returns (facts, field_set_used)."""
    facts: Dict[str, dict] = {}
    field_set = "full"
    for i in range(0, len(gids), _NODES_BATCH):
        batch = gids[i:i + _NODES_BATCH]
        try:
            data = gql(_FACTS_GQL_FULL if field_set == "full" else _FACTS_GQL_MIN,
                       {"ids": batch}, token, domain)
        except Exception as exc:
            if field_set != "full":
                raise
            logger.warning(f"[ShopifyDiag] full fact query rejected ({exc}) — retrying with status+tags only")
            field_set = "minimal"
            data = gql(_FACTS_GQL_MIN, {"ids": batch}, token, domain)
        for node in (data.get("data") or {}).get("nodes") or []:
            if node and node.get("id"):
                facts[node["id"]] = node
    return facts, field_set


def _shopify_tag_ids(raw_tag: str, token: str, domain: str, gql) -> Tuple[Set[str], bool]:
    """Product ids Shopify's own search returns for tag:"<raw_tag>" (all statuses)."""
    escaped = raw_tag.replace("\\", "\\\\").replace('"', '\\"')
    query = f'tag:"{escaped}"'
    ids: Set[str] = set()
    cursor = None
    while len(ids) < _TAG_SEARCH_CAP:
        data = gql(_TAG_IDS_GQL, {"query": query, "first": 250, "after": cursor}, token, domain)
        pdata = (data.get("data") or {}).get("products") or {}
        edges = pdata.get("edges") or []
        ids.update(e["node"]["id"] for e in edges if e.get("node"))
        pi = pdata.get("pageInfo") or {}
        if not pi.get("hasNextPage") or not edges:
            return ids, False
        cursor = pi.get("endCursor")
    return ids, True


# ── filter-tree helpers ──────────────────────────────────────────────────────

def _tag_filter_values(node: dict) -> List[str]:
    """Every positive tag value anywhere in the tree (negated tags are exclusions,
    reported through the per-condition results instead)."""
    out: List[str] = []
    if node.get("type") == "tag" and not node.get("negate"):
        out.extend(node.get("values") or [])
    for c in node.get("conditions") or []:
        out.extend(_tag_filter_values(c))
    return list(dict.fromkeys(out))


def _top_conditions(tree: dict) -> List[dict]:
    if not tree:
        return []
    if "type" in tree:
        return [tree]
    return list(tree.get("conditions") or [])


def _label(cond: dict) -> str:
    if "type" in cond:
        neg = "NOT " if cond.get("negate") else ""
        if cond["type"] == "option":
            return f"{neg}option:{cond.get('key')}={','.join(map(str, cond.get('values') or []))}"
        if cond["type"] in ("price", "available"):
            return f"{neg}{cond['type']}"
        return f"{neg}{cond['type']}:{','.join(map(str, cond.get('values') or []))}"
    inner = " ".join(_label(c) for c in cond.get("conditions") or [])
    return f"({cond.get('relation', 'AND')} {inner})"


# ── main entry ───────────────────────────────────────────────────────────────

def run(*, raw: List[dict], filtered: List[dict], filter_tree: dict,
        search_f: str, price_f: dict, stock_f: str,
        token: str, domain: str, layer1_source: str) -> None:
    # Imported here, not at module top: shopify_graphql_executor imports this
    # module, and these are only needed when the flag is on.
    from api_builder.shopify_graphql_executor import _gql, _evaluate, _norm_label

    kept_ids = {p.get("id") for p in filtered}
    gids = [p.get("id") for p in raw if p.get("id")]
    facts, field_set = _fetch_facts(gids, token, domain, _gql) if gids else ({}, "none")

    # Raw tag spellings on the candidates that MiraQ treats as each filter value.
    tag_values = _tag_filter_values(filter_tree or {})
    spellings: Dict[str, Dict[str, int]] = {v: {} for v in tag_values}
    for p in raw:
        live_tags = (facts.get(p.get("id")) or {}).get("tags", p.get("tags") or [])
        for t in live_tags:
            for v in tag_values:
                if _norm_label(t) == _norm_label(v):
                    spellings[v][t] = spellings[v].get(t, 0) + 1

    # Reference spelling per filter value: the most common raw spelling.
    reference: Dict[str, str] = {
        v: max(spellings[v].items(), key=lambda kv: kv[1])[0]
        for v in tag_values if spellings[v]
    }

    # Shopify's own answer for each spelling.
    shopify_tag: Dict[str, Tuple[Set[str], bool]] = {}
    for v in tag_values:
        for spelling in spellings[v]:
            if spelling not in shopify_tag:
                shopify_tag[spelling] = _shopify_tag_ids(spelling, token, domain, _gql)

    conds = _top_conditions(filter_tree or {})
    groups: Dict[str, List[str]] = {
        "not_active": [], "not_on_online_store": [], "tag_disagree": [],
        "stock_mismatch": [], "no_live_facts": [],
    }
    storefront_visible = 0

    for p in raw:
        gid = p.get("id")
        f = facts.get(gid)
        title = (f or {}).get("title") or p.get("title") or "?"
        kept = gid in kept_ids
        variants = p.get("variants") or []
        avail = sum(1 for v in variants if v.get("availableForSale"))

        parts = [f"{title!r} ({gid})", f"miraq={'KEPT' if kept else 'DROPPED'}"]

        if f is None:
            parts.append("live_facts=NOT_RETURNED (deleted, or not visible to this token)")
            if kept:
                groups["no_live_facts"].append(title)
        else:
            status = f.get("status")
            parts.append(f"status={status}")
            if field_set == "full":
                on_store = f.get("onlineStoreUrl") is not None
                parts.append(f"online_store={'yes' if on_store else 'NO'}")
                parts.append(f"published_at={f.get('publishedAt')}")
                parts.append(
                    f"total_inventory={f.get('totalInventory')} "
                    f"tracks_inventory={f.get('tracksInventory')}"
                )
            else:
                on_store = None
            if kept:
                if status != "ACTIVE":
                    groups["not_active"].append(title)
                if on_store is False:
                    groups["not_on_online_store"].append(title)
                if status == "ACTIVE" and on_store is not False:
                    storefront_visible += 1
                if (field_set == "full" and (f.get("totalInventory") or 0) <= 0 and avail > 0):
                    groups["stock_mismatch"].append(title)

        parts.append(f"variants_available={avail}/{len(variants)}")

        # Tag evidence: the raw spelling MiraQ matched, and Shopify's verdict on it.
        live_tags = (f or {}).get("tags", p.get("tags") or [])
        disagree = False
        for v in tag_values:
            matched = [t for t in live_tags if _norm_label(t) == _norm_label(v)]
            if not matched:
                parts.append(f"tag[{v}]: none")
                continue
            ref = reference[v]
            ids, truncated = shopify_tag.get(ref, (set(), False))
            hit = gid in ids
            verdict = "yes" if hit else ("unknown(truncated)" if truncated else "NO")
            parts.append(
                f"tag[{v}]: raw={', '.join(repr(t) for t in matched)} "
                f"shopify_search[{ref!r}]={verdict}"
            )
            if not hit and not truncated:
                disagree = True
        if kept and disagree:
            groups["tag_disagree"].append(title)

        # Which top-level condition passes for at least one variant.
        cond_res = []
        for c in conds:
            if c.get("type") == "collection" and not c.get("negate") and not c.get("enforce"):
                wanted = {_norm_label(x) for x in c.get("values") or []}
                have = {_norm_label(col.get("handle", "")) for col in p.get("collections") or []}
                have |= {_norm_label(col.get("title", "")) for col in p.get("collections") or []}
                cond_res.append(f"{_label(c)}={'member' if wanted & have else 'NOT_MEMBER(fetch-enforced)'}")
                continue
            ok = any(_evaluate(c, p, v) for v in variants) if variants else _evaluate(c, p, {})
            cond_res.append(f"{_label(c)}={'pass' if ok else 'FAIL'}")
        if search_f or price_f or stock_f:
            cond_res.append(f"also search={search_f!r} price={price_f} stock={stock_f!r}")
        parts.append("conds=[" + ", ".join(cond_res) + "]")

        logger.info("[ShopifyDiag] " + " | ".join(parts))

    spell_txt = "; ".join(
        f"{v} (reference {reference[v]!r}): " + ", ".join(
            f"{s!r}×{n} (shopify_search={len(shopify_tag[s][0])}{'+' if shopify_tag[s][1] else ''} ids)"
            for s, n in spellings[v].items()
        )
        for v in tag_values if spellings[v]
    ) or "none"

    def _names(k: str) -> str:
        return f"{k}={len(groups[k])}{groups[k][:15] if groups[k] else ''}"

    logger.info(
        f"[ShopifyDiag] summary | layer1={layer1_source} | facts={field_set} | "
        f"filter={_label(filter_tree) if filter_tree else 'none'} | "
        f"raw={len(raw)} miraq_kept={len(kept_ids)} | "
        f"kept_and_storefront_visible="
        f"{storefront_visible if field_set == 'full' else f'unknown(no online-store facts; kept_and_active={storefront_visible})'} | "
        f"{_names('not_active')} | {_names('not_on_online_store')} | "
        f"{_names('tag_disagree')} | {_names('stock_mismatch')} | "
        f"{_names('no_live_facts')} | tag_spellings: {spell_txt}"
    )
