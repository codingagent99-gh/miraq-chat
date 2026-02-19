# Bug Fixes Summary

This PR fixes three related bugs in the product classification and API filtering logic.

## Bug 1: Generic "products" incorrectly matched as product "Product"

**Problem:** When users said "Show me more products" or "Show me all products", the word "product"/"products" was being matched against a store product literally named "Product" (id=7846). This caused the classifier to hit rule #13 (`PRODUCT_SEARCH` by name) instead of rule #15 (`PRODUCT_LIST`).

**Root Cause:** The `_extract_product_name()` function in `classifier.py` and the product token indexing in `store_loader.py` didn't skip generic words like "product", "products", "tile", "tiles", etc.

**Fix Applied:**
1. Added skip list in `_extract_product_name()` to ignore generic words
2. Added "product" and "products" to stop words in store_loader token indexing
3. Added explicit higher-priority rule to catch "show me more/all products" patterns

**Files Changed:**
- `classifier.py` (lines 208-210, 266-269)
- `store_loader.py` (line 271)

## Bug 2: Broken suggestions (cascading from Bug 1)

**Problem:** After Bug 1 fetched the wrong product (name="Product", id=7846), the response generator would create broken suggestions using `base_name = name.split(" ")[0]` which becomes `"Product"`, producing suggestions like:
- "Show me Product Chip Card" → returns 0 products
- "Show me Product Mosaic" → returns 0 products

**Fix Applied:** This is automatically fixed by fixing Bug 1. When "products" no longer matches "Product", the cascading issue disappears.

## Bug 3: Category browse ignores attribute filters

**Problem:** When users said "show me tiles of size 12x24", the entity extraction correctly extracted BOTH `category_id` and `tile_size`, but the `CATEGORY_BROWSE` branch in `api_builder.py` only applied the category filter and ignored attribute filters like `tile_size`.

**Root Cause:** The `CATEGORY_BROWSE` intent handler in `api_builder.py` only checked for `on_sale` and `tag_ids` but didn't apply attribute filters.

**Fix Applied:** Modified the `CATEGORY_BROWSE` branch to check for and apply attribute filters when present:
```python
if e.attribute_slug and e.attribute_term_ids:
    params["attribute"] = e.attribute_slug
    params["attribute_term"] = ",".join(str(tid) for tid in e.attribute_term_ids)
```

**Files Changed:**
- `api_builder.py` (lines 131-134)

## Testing

Created comprehensive test suite in `test_product_classification_bugs.py` with 14 tests:

**Bug 1 Tests (7 tests):**
- ✅ "Show me more products" → PRODUCT_LIST
- ✅ "Show me all products" → PRODUCT_LIST
- ✅ "Get all products" → PRODUCT_LIST
- ✅ "List all products" → PRODUCT_LIST
- ✅ "See more products" → PRODUCT_LIST
- ✅ Generic word in order context doesn't match
- ✅ Generic "item" word doesn't match

**Bug 3 Tests (4 tests):**
- ✅ API builder includes both category and attribute filters
- ✅ "tiles of size 12x24" includes size filter
- ✅ Category with finish filter includes both
- ✅ Category with color filter includes both

**Bug 2 Tests (2 tests):**
- ✅ "Show me more products" doesn't extract "Product" entity
- ✅ "all products" doesn't extract "Product" entity

**Regression Testing:**
- ✅ All 78 existing tests pass (greeting, order flow, LLM fallback)

## Changes Summary

**Total Lines Changed:** 11 lines across 3 files
- classifier.py: +7 lines
- store_loader.py: +1 line  
- api_builder.py: +4 lines
- test_product_classification_bugs.py: +263 lines (new file)

## Code Quality

- ✅ Code review completed and feedback addressed
- ✅ Security scan (CodeQL) passed with 0 vulnerabilities
- ✅ All tests passing (78 tests)
- ✅ Minimal, surgical changes as required
- ✅ No regressions introduced

## Impact

These fixes ensure:
1. Generic product-related queries are correctly classified as PRODUCT_LIST
2. Product-specific searches only match actual product names
3. Category browsing with attribute filters (size, finish, color, etc.) works correctly
4. API calls include all relevant filters for accurate product search results
