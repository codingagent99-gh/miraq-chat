"""
catalog_compare.py - prove the plugin's public /catalog routes return the same
catalogue the backend used to read from /wc/v3 with the WooCommerce key.

Run it on staging (or the live store) BEFORE switching the backend over. It
reads everything twice, once from /wc/v3 with the key and once from
/wp-json/miraq/v1/catalog/... without it, and compares item by item:

  products        every published product (all pages)
  variations      for each variable product (status=publish and all)
  single items    /products/{id} for a sample of products and variations
  categories/tags hide_empty=true, as the loader reads them
  attribute terms for every attribute in /all-attributes
  currency

Verdicts
  VALUE DIFF      a field both sides return has a different value  -> must be 0
  MISSING/EXTRA   an item one side has and the other does not       -> must be 0
  BLANKED         description blanked on a password-protected item  -> expected
  REMOVED         fields /wc/v3 returns that /catalog leaves out    -> check the
                  list is only private fields (sales, stock, meta, downloads...)

Usage
    python catalog_compare.py --site https://store.example.com --key ck_... --secret cs_...
        [--namespace miraq/v1] [--sample 25] [--out catalog_compare_report.json]

Exit code 1 if there is any VALUE DIFF or MISSING/EXTRA item.
"""

import argparse
import json
import sys
import time
from collections import Counter

import requests


class Compare:
    def __init__(self, site, key, secret, namespace):
        self.wc = f"{site.rstrip('/')}/wp-json/wc/v3"
        self.cat = f"{site.rstrip('/')}/wp-json/{namespace.strip('/')}"
        self.auth = (key, secret)
        self.s = requests.Session()
        self.value_diffs, self.missing, self.extra, self.blanked = [], [], [], []
        self.removed = Counter()
        self.checked = Counter()

    # ── fetching ──
    def _get(self, url, params=None, auth=True):
        r = self.s.get(url, params=params or {}, auth=self.auth if auth else None, timeout=60)
        r.raise_for_status()
        return r

    def _all(self, url, params=None, auth=True):
        items, page = [], 1
        while True:
            r = self._get(url, {**(params or {}), "per_page": 100, "page": page}, auth)
            data = r.json()
            if not data:
                break
            items.extend(data)
            if page >= int(r.headers.get("X-WP-TotalPages", 1)):
                break
            page += 1
            time.sleep(0.2)
        return items

    # ── comparing ──
    def _compare_item(self, where, old, new):
        self.checked[where.split(" ")[0]] += 1
        for field, new_val in new.items():
            if field not in old:
                self.value_diffs.append({"where": where, "field": field, "old": "<absent>", "new": new_val})
                continue
            old_val = old[field]
            if field == "meta_data":
                keep = {m.get("key") for m in new_val}
                old_val = [m for m in old_val if m.get("key") in keep]
            if old_val != new_val:
                if field in ("description", "short_description") and new_val == "" and old_val:
                    self.blanked.append({"where": where, "field": field})
                    continue
                self.value_diffs.append({"where": where, "field": field, "old": old_val, "new": new_val})
        for field in old:
            if field not in new and field != "_links":
                self.removed[field] += 1

    def _compare_lists(self, where, old_items, new_items):
        old_by, new_by = {i["id"]: i for i in old_items}, {i["id"]: i for i in new_items}
        for i in old_by.keys() - new_by.keys():
            self.missing.append({"where": where, "id": i})
        for i in new_by.keys() - old_by.keys():
            self.extra.append({"where": where, "id": i})
        for i in old_by.keys() & new_by.keys():
            self._compare_item(f"{where} #{i}", old_by[i], new_by[i])
        return old_by

    def run(self, sample):
        print("products ...")
        old_products = self._all(f"{self.wc}/products", {"status": "publish"})
        new_products = self._all(f"{self.cat}/catalog/products", {"status": "publish"}, auth=False)
        by_id = self._compare_lists("products", old_products, new_products)

        variable = [p["id"] for p in old_products if p.get("type") == "variable"]
        print(f"variations for {len(variable)} variable products ...")
        first_variations = []
        for pid in variable:
            for status in ("publish", None):
                params = {"status": status} if status else {}
                old_v = self._all(f"{self.wc}/products/{pid}/variations", params)
                if not status:  # /wc/v3 "any" includes drafts/trash; /catalog never returns those
                    old_v = [v for v in old_v if v.get("status") in ("publish", "private")]
                new_v = self._all(f"{self.cat}/catalog/products/{pid}/variations", params, auth=False)
                self._compare_lists(f"variations({status or 'all'}) of {pid}", old_v, new_v)
                if old_v and status == "publish":
                    first_variations.append((pid, old_v[0]["id"]))

        print(f"single items (sample {sample}) ...")
        for pid in list(by_id)[:sample]:
            self._compare_item(f"product {pid}", self._get(f"{self.wc}/products/{pid}").json(),
                               self._get(f"{self.cat}/catalog/products/{pid}", auth=False).json())
        for pid, vid in first_variations[:sample]:
            self._compare_item(f"variation {pid}/{vid}",
                               self._get(f"{self.wc}/products/{pid}/variations/{vid}").json(),
                               self._get(f"{self.cat}/catalog/products/{pid}/variations/{vid}", auth=False).json())
            self._compare_item(f"product-as-variation {vid}",
                               self._get(f"{self.wc}/products/{vid}").json(),
                               self._get(f"{self.cat}/catalog/products/{vid}", auth=False).json())

        print("categories, tags ...")
        for kind in ("categories", "tags"):
            self._compare_lists(kind, self._all(f"{self.wc}/products/{kind}", {"hide_empty": "true"}),
                                self._all(f"{self.cat}/catalog/{kind}", {"hide_empty": "true"}, auth=False))

        print("attribute terms ...")
        for attr in self._get(f"{self.wc}/products/attributes").json():
            aid = attr["id"]
            self._compare_lists(f"terms of attribute {aid}", self._all(f"{self.wc}/products/attributes/{aid}/terms"),
                                self._all(f"{self.cat}/catalog/attributes/{aid}/terms", auth=False))

        print("currency ...")
        self._compare_item("currency", self._get(f"{self.wc}/data/currencies/current").json(),
                           self._get(f"{self.cat}/catalog/currency", auth=False).json())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--namespace", default="miraq/v1")
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--out", default="catalog_compare_report.json")
    args = ap.parse_args()

    c = Compare(args.site, args.key, args.secret, args.namespace)
    c.run(args.sample)

    report = {"checked": dict(c.checked), "value_diffs": c.value_diffs, "missing": c.missing,
              "extra": c.extra, "blanked": c.blanked, "removed_fields": dict(c.removed)}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nchecked: {dict(c.checked)}")
    print(f"VALUE DIFF: {len(c.value_diffs)} | MISSING: {len(c.missing)} | EXTRA: {len(c.extra)} | "
          f"BLANKED (password-protected): {len(c.blanked)}")
    print(f"REMOVED fields (confirm these are all private): {sorted(c.removed)}")
    for d in c.value_diffs[:20]:
        print(f"  DIFF {d['where']} . {d['field']}: old={str(d['old'])[:80]!r} new={str(d['new'])[:80]!r}")
    for m in (c.missing + c.extra)[:20]:
        print(f"  {'MISSING' if m in c.missing else 'EXTRA'} {m['where']} id={m['id']}")
    print(f"\nfull report -> {args.out}")
    sys.exit(1 if (c.value_diffs or c.missing or c.extra) else 0)


if __name__ == "__main__":
    main()