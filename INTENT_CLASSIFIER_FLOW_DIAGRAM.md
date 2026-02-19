# Intent Classifier - Visual Flow Diagram

## Complete Classification Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER INPUT                                   │
│              "Show me 12x24 matte tiles in stock"                   │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    STEP 1: TEXT NORMALIZATION                        │
│  • Convert to lowercase: "show me 12x24 matte tiles in stock"      │
│  • Strip extra whitespace                                           │
│  • Prepare for pattern matching                                     │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│              STEP 2: ENTITY EXTRACTION (Parallel)                    │
│  ┌────────────────────┬────────────────────┬──────────────────────┐ │
│  │ _extract_category()│ _extract_size()    │ _extract_finish()    │ │
│  │ → category_id=42   │ → tile_size="12x24"│ → finish="Matte"     │ │
│  │ → category="Tiles" │ → attr_slug=       │ → attr_slug=         │ │
│  │                    │   "pa_tile-size"   │   "pa_finish"        │ │
│  └────────────────────┴────────────────────┴──────────────────────┘ │
│  ┌────────────────────┬────────────────────┬──────────────────────┐ │
│  │ _extract_product() │ _extract_quantity()│ _extract_quick_ship()│ │
│  │ → product_name=None│ → quantity=None    │ → quick_ship=True    │ │
│  └────────────────────┴────────────────────┴──────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│          STEP 3: INTENT CLASSIFICATION (Priority Order)              │
│                                                                      │
│  Priority 1: GREETINGS ───────────────────────────────────┐ NO     │
│              "hi", "hello", "good morning"                 │         │
│                                                            └───────▶│
│  Priority 2: ORDERS/REORDERS ──────────────────────────────┐ NO    │
│              "reorder", "order again"                       │        │
│                                                             └──────▶│
│  Priority 3: ORDER TRACKING ───────────────────────────────┐ NO    │
│              "track my order"                               │        │
│                                                             └──────▶│
│  Priority 4: PROMOTIONS ───────────────────────────────────┐ NO    │
│              "discount", "on sale"                          │        │
│                                                             └──────▶│
│  Priority 5: SAMPLES ──────────────────────────────────────┐ NO    │
│              "sample", "chip card"                          │        │
│                                                             └──────▶│
│  Priority 6: PRODUCT VARIATIONS ───────────────────────────┐ NO    │
│              "what colors available"                        │        │
│                                                             └──────▶│
│  Priority 7: CATEGORY MATCH ───────────────────────────────┐ YES!  │
│              Check if category_id exists                    │        │
│              → Yes! category_id = 42                        │        │
│              Check if attributes exist                      │        │
│              → Yes! tile_size, finish, quick_ship           │        │
│              ┌─────────────────────────────────────────┐    │        │
│              │ COMBINED DETECTION LOGIC:               │    │        │
│              │ if category + product + attributes:     │    │        │
│              │   → PRODUCT_SEARCH_IN_CATEGORY (0.96)  │    │        │
│              │ elif category + product:                │    │        │
│              │   → PRODUCT_SEARCH_IN_CATEGORY (0.95)  │    │        │
│              │ elif category + attributes:             │    │        │
│              │   → CATEGORY_BROWSE_FILTERED (0.95) ✓  │    │        │
│              │ else:                                   │    │        │
│              │   → CATEGORY_BROWSE (0.94)             │    │        │
│              └─────────────────────────────────────────┘    │        │
│                                                             └──────▶│
│  RESULT: Intent = CATEGORY_BROWSE_FILTERED                          │
│          Confidence = 0.95                                           │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│            STEP 4: LLM FALLBACK CHECK (if needed)                    │
│  Trigger conditions:                                                 │
│  • Intent = UNKNOWN                                                  │
│  • Confidence < 0.85                                                 │
│  • Missing critical entities                                         │
│                                                                      │
│  Current state:                                                      │
│  • Intent = CATEGORY_BROWSE_FILTERED ✓                              │
│  • Confidence = 0.95 ✓                                              │
│  • Entities populated ✓                                             │
│                                                                      │
│  Decision: SKIP LLM FALLBACK (not needed)                           │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│              STEP 5: API CALL GENERATION                             │
│                                                                      │
│  Input to API Builder:                                               │
│  • Intent: CATEGORY_BROWSE_FILTERED                                 │
│  • Entities: {                                                       │
│      category_id: 42,                                               │
│      category_name: "Tiles",                                        │
│      tile_size: "12x24",                                            │
│      finish: "Matte",                                               │
│      quick_ship: true,                                              │
│      attribute_slug: "pa_tile-size"                                 │
│    }                                                                 │
│                                                                      │
│  Generated API Calls:                                                │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ CALL 1: Category Browse                                     │   │
│  │ GET /products?category=42&per_page=20&status=publish        │   │
│  │ Description: "Browse category 'Tiles'"                      │   │
│  └─────────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ CALL 2: Attribute Filter (Custom API)                       │   │
│  │ GET /products-by-attribute                                   │   │
│  │ Params: {                                                    │   │
│  │   filters: [                                                 │   │
│  │     {"attribute":"pa_tile-size", "terms":"12x24"},          │   │
│  │     {"attribute":"pa_finish", "terms":"Matte"},             │   │
│  │     {"attribute":"category", "terms":"42"}                  │   │
│  │   ],                                                         │   │
│  │   page: 1                                                    │   │
│  │ }                                                            │   │
│  │ Description: "Filter by size & finish in category"          │   │
│  │ is_custom_api: true                                          │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│              STEP 6: EXECUTE WOOCOMMERCE API                         │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Execute CALL 1:                                              │  │
│  │ HTTP GET https://wgc.net.in/.../products?category=42         │  │
│  │ Headers: Browser-like User-Agent                             │  │
│  │ Auth: Query-string (consumer_key, consumer_secret)           │  │
│  │ Response: 156 products in category                           │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Execute CALL 2:                                              │  │
│  │ HTTP GET .../products-by-attribute?filters=...               │  │
│  │ Response: 23 products matching filters                       │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Logging:                                                            │
│  • Request: method, endpoint, params, description, custom_api        │
│  • Response: status, result_count, total, response_time_ms          │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│            STEP 7: POST-API LLM FALLBACK CHECK                       │
│  Trigger conditions:                                                 │
│  • API returned 0 products                                           │
│  • LLM_RETRY_ON_EMPTY_RESULTS = True                                │
│                                                                      │
│  Current state:                                                      │
│  • Got 23 products ✓                                                │
│                                                                      │
│  Decision: SKIP POST-API FALLBACK (results found)                   │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│              STEP 8: FORMAT PRODUCTS                                 │
│  For each product:                                                   │
│  • Extract: id, name, price, sale_price, images                     │
│  • Add: attributes (size, finish, color), stock_status              │
│  • Format: variations if applicable                                 │
│  • Filter: by entities (if needed)                                  │
│                                                                      │
│  Example formatted product:                                          │
│  {                                                                   │
│    "id": 1234,                                                       │
│    "name": "Carrara Marble 12x24 Matte",                            │
│    "price": 8.99,                                                    │
│    "images": [{...}],                                                │
│    "attributes": {                                                   │
│      "pa_tile-size": "12x24",                                        │
│      "pa_finish": "Matte"                                            │
│    },                                                                │
│    "in_stock": true                                                  │
│  }                                                                   │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│            STEP 9: GENERATE BOT RESPONSE                             │
│  Components:                                                         │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ bot_message:                                                 │  │
│  │ "Here are 23 products in Tiles filtered by 12x24 / Matte!"  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ suggestions: [                                               │  │
│  │   "Show me polished finish",                                 │  │
│  │   "Show me 24x48 size",                                      │  │
│  │   "What's on sale?",                                         │  │
│  │   "Show me quick ship products"                              │  │
│  │ ]                                                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ filters_applied: {                                           │  │
│  │   "category": "Tiles",                                       │  │
│  │   "size": "12x24",                                           │  │
│  │   "finish": "Matte",                                         │  │
│  │   "quick_ship": true                                         │  │
│  │ }                                                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│              STEP 10: RETURN JSON RESPONSE                           │
│  {                                                                   │
│    "success": true,                                                  │
│    "bot_message": "Here are 23 products...",                         │
│    "intent": "category",                                             │
│    "products": [{...}, {...}, ...],  // 23 products                 │
│    "filters_applied": {...},                                         │
│    "suggestions": [...],                                             │
│    "session_id": "session_abc123",                                   │
│    "metadata": {                                                     │
│      "intent_raw": "category_browse_filtered",                       │
│      "confidence": 0.95,                                             │
│      "entities_extracted": {                                         │
│        "category_id": 42,                                            │
│        "tile_size": "12x24",                                         │
│        "finish": "Matte",                                            │
│        "quick_ship": true                                            │
│      },                                                              │
│      "api_calls_made": 2,                                            │
│      "total_time_ms": 847,                                           │
│      "llm_fallback_used": false                                      │
│    }                                                                 │
│  }                                                                   │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                      ┌───────────────────┐
                      │  DISPLAY TO USER  │
                      └───────────────────┘
```

## Key Decision Points

### Decision 1: Entity Extraction
**What happens:** All extraction functions run in sequence
**Result:** Entities object populated with all found values
**Impact:** Influences intent classification

### Decision 2: Intent Priority
**What happens:** Checks patterns in priority order (1-10)
**Result:** First matching pattern with highest priority wins
**Impact:** Determines which API calls to make

### Decision 3: Combined Intent Detection
**What happens:** Special logic for category + product/attributes
**Result:** Upgraded to more specific intents (FILTERED, IN_CATEGORY)
**Impact:** More precise API calls, better results

### Decision 4: LLM Pre-API Fallback
**What happens:** Checks if classification is uncertain
**Result:** May invoke GPT-4 for better understanding
**Impact:** Higher accuracy for complex queries

### Decision 5: API Call Strategy
**What happens:** Intent + entities mapped to API calls
**Result:** 1-3 API calls generated
**Impact:** Balances speed vs. completeness

### Decision 6: LLM Post-API Fallback
**What happens:** Checks if results are empty
**Result:** May retry with LLM suggestions
**Impact:** Better handling of "no results" scenarios

---

## Performance Metrics for This Flow

```
Step 1: Normalization        ~1ms
Step 2: Entity Extraction     ~15ms
Step 3: Classification        ~8ms
Step 4: LLM Check            ~0ms (skipped)
Step 5: API Building         ~2ms
Step 6: API Execution        ~800ms (network I/O)
Step 7: LLM Check            ~0ms (skipped)
Step 8: Formatting           ~15ms
Step 9: Response Gen         ~5ms
Step 10: JSON Serialization  ~1ms
─────────────────────────────────
Total:                       ~847ms
```

**Breakdown:**
- Classification logic: ~26ms (3%)
- API calls: ~800ms (94%)
- Other: ~21ms (3%)

**Bottleneck:** Network I/O to WooCommerce
**Optimization:** Could cache common queries, use CDN

---
