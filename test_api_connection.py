"""
Quick diagnostic script — tests your WooCommerce API connection.
Run this FIRST to find the working auth method.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
CK = os.getenv("WOO_CONSUMER_KEY", "")
CS = os.getenv("WOO_CONSUMER_SECRET", "")

print("=" * 60)
print("WooCommerce API Connection Test")
print("=" * 60)
print(f"Base URL: {BASE}")
print(f"Key:      {CK[:12]}..." if CK else "Key: ❌ NOT SET")
print(f"Secret:   {CS[:8]}..." if CS else "Secret: ❌ NOT SET")
print()

test_url = f"{BASE}/products/categories"

# ─── Test 1: Query String Auth ───
print("━" * 60)
print("Test 1: Query String Auth (consumer_key in URL)")
try:
    r = requests.get(
        test_url,
        params={
            "per_page": 3,
            "consumer_key": CK,
            "consumer_secret": CS,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    print(f"   Status: {r.status_code}")
    print(f"   Headers: {dict(list(r.headers.items())[:5])}")
    if r.status_code == 200:
        data = r.json()
        print(f"   ✅ SUCCESS! Got {len(data)} categories")
        for c in data[:3]:
            print(f"      • {c.get('name')} (id={c.get('id')}, count={c.get('count')})")
    else:
        print(f"   ❌ Failed: {r.text[:200]}")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── Test 2: Basic Auth ───
print()
print("━" * 60)
print("Test 2: HTTP Basic Auth")
try:
    r = requests.get(
        test_url,
        params={"per_page": 3},
        auth=(CK, CS),
        headers={"Accept": "application/json"},
        timeout=15,
    )
    print(f"   Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"   ✅ SUCCESS! Got {len(data)} categories")
    else:
        print(f"   ❌ Failed: {r.text[:200]}")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── Test 3: Browser-like User-Agent ───
print()
print("━" * 60)
print("Test 3: Browser User-Agent + Query Auth")
try:
    r = requests.get(
        test_url,
        params={
            "per_page": 3,
            "consumer_key": CK,
            "consumer_secret": CS,
        },
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
        },
        timeout=15,
    )
    print(f"   Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"   ✅ SUCCESS! Got {len(data)} categories")
    else:
        print(f"   ❌ Failed: {r.text[:200]}")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── Test 4: No Auth (public) ───
print()
print("━" * 60)
print("Test 4: No Auth (public endpoint)")
try:
    r = requests.get(
        test_url,
        params={"per_page": 3},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    print(f"   Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"   ✅ SUCCESS! Got {len(data)} categories")
    else:
        print(f"   ❌ Failed: {r.text[:200]}")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── Test 5: Try /products endpoint instead ───
print()
print("━" * 60)
print("Test 5: GET /products (different endpoint)")
try:
    r = requests.get(
        f"{BASE}/products",
        params={
            "per_page": 1,
            "consumer_key": CK,
            "consumer_secret": CS,
        },
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        },
        timeout=15,
    )
    print(f"   Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"   ✅ SUCCESS! Got {len(data)} product(s)")
        if data:
            print(f"      • {data[0].get('name')} (id={data[0].get('id')})")
            cats = data[0].get("categories", [])
            print(f"      • Categories: {[c.get('name') for c in cats]}")
    else:
        print(f"   ❌ Failed: {r.text[:200]}")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── Test 6: Raw curl-like test ───
print()
print("━" * 60)
print("Test 6: Manual URL test")
manual_url = f"{BASE}/products/categories?per_page=3&consumer_key={CK}&consumer_secret={CS}"
print(f"   Try opening this URL in your browser:")
print(f"   {manual_url[:80]}...")
print()
print("=" * 60)
print("DONE — Check which test(s) passed above.")
print("The first one that returns 200 is the auth method to use.")
print("=" * 60)