"""
orders_compare.py - prove the plugin's /my-orders routes return the same
orders the backend used to read from /wc/v3 with the WooCommerce key, and
nothing that belongs to anyone else.

Run it on staging (or the live store) with one real customer who has orders.
It needs that customer's identity token (the backend cannot make one: only
the store holds the site key) and, for the /wc/v3 side, a WooCommerce key
you create for this check and delete afterwards. Then it compares, order by
order:

  list      /wc/v3/orders?customer=<id>  vs  /my-orders          (all pages)
  dates     the same, with after/before
  single    /wc/v3/orders/<id>           vs  /my-orders/<id>     (sample)
  isolation another customer's order, and a missing token  -> must be refused

Verdicts
  VALUE DIFF      a field both sides return has a different value  -> must be 0
  MISSING/EXTRA   an order one side has and the other does not       -> must be 0
  NOT REFUSED     someone else's order or a tokenless call came back -> must be 0
  REMOVED         fields /wc/v3 returns that /my-orders leaves out   -> check the
                  list is only private fields (meta_data, order_key, ip, ...)

Usage
    python orders_compare.py --site https://store.example.com --key ck_... --secret cs_...
        --token <identity token>
        [--other-order <an order id of a DIFFERENT customer>]
        [--after 2026-01-01T00:00:00 --before 2026-06-30T23:59:59]
        [--namespace miraq/v1] [--sample 10] [--out orders_compare_report.json]

Getting the token: log in to the store as that customer, open any page, and
in the browser console run   copy(window.__silfraWidgetConfig.identityToken)   — or open
/wp-json/miraq/v1/identity in a logged-in tab that sends the wp_rest nonce.
It is valid for 12 hours. The customer id is read from the token.

The WooCommerce key: WooCommerce > Settings > Advanced > REST API > Add key,
Read permission, for an administrator. Delete it when you are done.
Exit code 1 if there is any VALUE DIFF, MISSING/EXTRA or NOT REFUSED.
"""

import argparse
import base64
import json
import sys
import time
from collections import Counter

import requests


def _token_customer_id(token: str) -> int:
    body = token.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return int(payload.get("cid") or 0)


class Compare:
    def __init__(self, site, key, secret, token, namespace):
        self.wc = f"{site.rstrip('/')}/wp-json/wc/v3"
        self.mine = f"{site.rstrip('/')}/wp-json/{namespace.strip('/')}/my-orders"
        self.auth = (key, secret)
        self.token = token
        self.customer = _token_customer_id(token)
        self.s = requests.Session()
        self.value_diffs, self.missing, self.extra, self.not_refused = [], [], [], []
        self.removed = Counter()
        self.checked = Counter()

    # ── fetching ──
    def _old(self, path, params=None):
        r = self.s.get(f"{self.wc}{path}", params=params or {}, auth=self.auth, timeout=60)
        r.raise_for_status()
        return r

    def _new(self, path="", params=None, token=True):
        headers = {"X-MiraQ-Identity": self.token} if token else {}
        return self.s.get(f"{self.mine}{path}", params=params or {}, headers=headers, timeout=60)

    def _all(self, fetch, params):
        items, page = [], 1
        while True:
            r = fetch({**params, "per_page": 100, "page": page})
            r.raise_for_status()
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
            elif old[field] != new_val:
                self.value_diffs.append({"where": where, "field": field, "old": old[field], "new": new_val})
        for field in old:
            if field not in new and field != "_links":
                self.removed[field] += 1

    def _compare_lists(self, where, old_items, new_items):
        old_by, new_by = {o["id"]: o for o in old_items}, {o["id"]: o for o in new_items}
        for i in old_by.keys() - new_by.keys():
            self.missing.append({"where": where, "id": i})
        for i in new_by.keys() - old_by.keys():
            self.extra.append({"where": where, "id": i})
        for i in old_by.keys() & new_by.keys():
            self._compare_item(f"{where} #{i}", old_by[i], new_by[i])
        if [o["id"] for o in old_items] != [o["id"] for o in new_items] and not (self.missing or self.extra):
            self.value_diffs.append({"where": where, "field": "<order>", "old": "newest first", "new": "different order"})
        return list(old_by)

    def run(self, sample, after, before, other_order):
        print("order list ...")
        ids = self._compare_lists(
            "list",
            self._all(lambda p: self._old("/orders", {**p, "customer": self.customer}), {}),
            self._all(lambda p: self._new("", p), {}),
        )
        for o in self._all(lambda p: self._new("", p), {}):
            if int(o.get("customer_id") or 0) != self.customer:
                self.not_refused.append({"where": "list", "id": o["id"], "customer_id": o.get("customer_id")})

        if after or before:
            print("order list with dates ...")
            dates = {k: v for k, v in (("after", after), ("before", before)) if v}
            self._compare_lists(
                "dates",
                self._all(lambda p: self._old("/orders", {**p, "customer": self.customer, **dates}), {}),
                self._all(lambda p: self._new("", {**p, **dates}), {}),
            )

        print(f"single orders (sample {sample}) ...")
        for oid in ids[:sample]:
            r = self._new(f"/{oid}")
            r.raise_for_status()
            self._compare_item(f"order {oid}", self._old(f"/orders/{oid}").json(), r.json())

        print("isolation ...")
        if ids:
            r = self._new(f"/{ids[0]}", token=False)
            if r.status_code != 401:
                self.not_refused.append({"where": "no token", "id": ids[0], "status": r.status_code})
            r = self._new("", {"page": 1}, token=False)
            if r.status_code != 401:
                self.not_refused.append({"where": "no token (list)", "status": r.status_code})
        if other_order:
            r = self._new(f"/{other_order}")
            if r.status_code != 404:
                self.not_refused.append({"where": "other customer's order", "id": other_order, "status": r.status_code})
            r = self._new("", {"include": other_order})
            if r.status_code != 200 or r.json():
                self.not_refused.append({"where": "other customer's order via include", "id": other_order})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--token", required=True, help="the customer's identity token")
    ap.add_argument("--other-order", type=int, default=0)
    ap.add_argument("--after", default="")
    ap.add_argument("--before", default="")
    ap.add_argument("--namespace", default="miraq/v1")
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--out", default="orders_compare_report.json")
    args = ap.parse_args()

    c = Compare(args.site, args.key, args.secret, args.token, args.namespace)
    if not c.customer:
        sys.exit("the token names no customer")
    c.run(args.sample, args.after, args.before, args.other_order)

    report = {"checked": dict(c.checked), "value_diffs": c.value_diffs, "missing": c.missing,
              "extra": c.extra, "not_refused": c.not_refused, "removed_fields": dict(c.removed)}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nchecked: {dict(c.checked)}")
    print(f"VALUE DIFF: {len(c.value_diffs)} | MISSING: {len(c.missing)} | EXTRA: {len(c.extra)} | "
          f"NOT REFUSED: {len(c.not_refused)}")
    print(f"REMOVED fields (confirm these are all private): {sorted(c.removed)}")
    for d in c.value_diffs[:20]:
        print(f"  DIFF {d['where']} . {d['field']}: old={str(d['old'])[:80]!r} new={str(d['new'])[:80]!r}")
    for m in (c.missing + c.extra)[:20]:
        print(f"  {'MISSING' if m in c.missing else 'EXTRA'} {m['where']} id={m['id']}")
    for n in c.not_refused:
        print(f"  NOT REFUSED {n}")
    print(f"\nfull report -> {args.out}")
    sys.exit(1 if (c.value_diffs or c.missing or c.extra or c.not_refused) else 0)


if __name__ == "__main__":
    main()
