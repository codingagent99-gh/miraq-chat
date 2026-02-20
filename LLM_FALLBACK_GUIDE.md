# LLM Fallback Module - Usage Guide

## Overview

The LLM Fallback module provides intelligent fallback capabilities when the regex-based classifier fails or returns low confidence results. It uses a configurable LLM (Large Language Model) to:

1. **Pre-API Fallback (Step 1.5)**: Interpret ambiguous user messages before querying WooCommerce
2. **Post-API Retry (Step 3.8)**: Suggest alternatives when searches return no products

## Architecture

### Components

1. **`llm_fallback.py`**: Core module with LLM client and fallback logic
2. **`app_config.py`**: Configuration settings (all via environment variables)
3. **`routes/chat.py`**: Integration points at Step 1.5 and Step 3.8
4. **`test_llm_fallback.py`**: Comprehensive test suite

### Privacy-First Design

- **ONLY sends public store data**: Product names, categories, attributes, tags
- **NEVER sends**: Customer IDs, emails, order history, payment info
- **PII sanitization**: Strips emails, phone numbers, credit cards before sending to LLM

## Configuration

All settings are configured via environment variables:

### Required Settings

```bash
# LLM Provider (mistral, copilot, openai, anthropic, azure_openai)
LLM_PROVIDER=mistral

# Model to use
LLM_MODEL=mistral-large-latest

# API credentials (depends on provider)
LLM_API_KEY=your-mistral-key-here  # For Mistral/OpenAI/Anthropic/Azure
# OR
COPILOT_API_TOKEN=your-token-here  # For Copilot
LLM_API_BASE_URL=https://...       # Optional, for custom endpoints
```

### Optional Settings

```bash
# Behavior
LLM_TEMPERATURE=0.3                # Lower = more deterministic (0.0-1.0)
LLM_MAX_TOKENS=500                 # Maximum tokens in response
LLM_TIMEOUT_SECONDS=10             # API call timeout

# Feature Flags
LLM_FALLBACK_ENABLED=true          # Master kill switch
LLM_RETRY_ON_EMPTY_RESULTS=true    # Enable post-API retry

# Cost Tracking
LLM_COST_PER_1K_INPUT=0.002        # USD per 1000 input tokens
LLM_COST_PER_1K_OUTPUT=0.008       # USD per 1000 output tokens
```

## Supported Providers

### 1. Mistral AI Cloud (Default)

```bash
LLM_PROVIDER=mistral
LLM_MODEL=mistral-large-latest
LLM_API_KEY=your-mistral-api-key
```

### 2. GitHub Copilot

```bash
LLM_PROVIDER=copilot
LLM_MODEL=gpt-5.2
COPILOT_API_TOKEN=your-copilot-token
```

### 3. OpenAI

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4
LLM_API_KEY=sk-your-openai-key
```

### 4. Anthropic Claude

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-sonnet-20240229
LLM_API_KEY=your-anthropic-key
```

### 5. Azure OpenAI

```bash
LLM_PROVIDER=azure_openai
LLM_MODEL=gpt-4
LLM_API_KEY=your-azure-key
LLM_API_BASE_URL=https://your-resource.openai.azure.com/openai/deployments/your-deployment/chat/completions?api-version=2023-05-15
```

## How It Works

### Step 1.5: Pre-API Fallback

Triggered when:
- Intent is `UNKNOWN`
- Confidence < 0.60 (configurable via `LOW_CONFIDENCE_THRESHOLD`)
- Search intent missing both `product_name` and `category_id`
- Order intent missing both `order_item_name` and `product_name`

**Flow:**
1. Sanitize user message (remove PII)
2. Build store context (products, categories, attributes, tags)
3. Construct system prompt with store data
4. Call LLM with sanitized message
5. Parse LLM response (JSON with intent, entities, bot_message)
6. Handle response type:
   - `intent_resolved`: Re-inject into pipeline at Step 2
   - `entity_extracted`: Merge entities and continue
   - `conversational`: Return response directly
7. If LLM fails, fall back to disambiguation menu

**Example:**

```
User: "do you have anything nice for my kitchen"

Classifier: UNKNOWN, confidence=0.0

LLM Response:
{
  "intent": "filter_by_application",
  "entities": {"application": "kitchen"},
  "bot_message": "I found some beautiful tiles for your kitchen!",
  "confidence": 0.85,
  "fallback_type": "intent_resolved"
}

Result: Continues to Step 2 with resolved intent and entities
```

### Step 3.8: Post-API Retry

Triggered when:
- API returns 0 products
- Intent is a search/filter type
- `LLM_RETRY_ON_EMPTY_RESULTS=true`
- `LLM_FALLBACK_ENABLED=true`

**Flow:**
1. Call LLM with original message and entities
2. Parse retry response:
   - `corrected_search`: LLM suggests a corrected term, retry search
   - `suggestion`: LLM provides helpful message, return to user
3. If corrected search still returns 0 products, use suggestion message

**Example:**

```
User: "show me marbel tiles"  (misspelled)

API: 0 products found

LLM Response:
{
  "retry_type": "corrected_search",
  "corrected_term": "marble tiles",
  "suggestion_message": "Did you mean 'marble tiles'? Let me search for that."
}

Result: Re-searches with "marble tiles", returns corrected results
```

## Logging

Every LLM call logs comprehensive metrics:

### Trigger Log

```
INFO | Step 1.5: LLM fallback triggered | session=sess_abc | reason=unknown_intent | 
original_intent=unknown | confidence=0.00 | message="do you have anything nice for my kitchen"
```

### API Call Log

```
INFO | Step 1.5: LLM API call | model=gpt-5.2 | input_tokens=842 | output_tokens=156 | 
total_tokens=998 | latency_ms=1230 | cost_estimate=$0.0024
```

### Resolution Log

```
INFO | Step 1.5: LLM fallback resolved | fallback_type=intent_resolved | 
resolved_intent=product_search | resolved_entities={"application": "kitchen"} | 
new_confidence=0.85
```

### Response Metadata

The API response includes LLM metadata:

```json
{
  "metadata": {
    "provider": "llm_fallback",
    "llm_model": "gpt-5.2",
    "llm_tokens_used": 998,
    "llm_input_tokens": 842,
    "llm_output_tokens": 156,
    "llm_latency_ms": 1230,
    "llm_cost_estimate": 0.0024,
    "llm_fallback_reason": "unknown_intent",
    "llm_trigger_reason": "unknown_intent",
    "original_intent": "unknown",
    "original_confidence": 0.0
  }
}
```

## Testing

### Run Unit Tests

```bash
python -m pytest test_llm_fallback.py -v
```

### Test Coverage

- **PII Sanitization**: Email, phone, credit card, SSN removal
- **Store Context Building**: Product/category/attribute extraction
- **System Prompt Construction**: Proper formatting and privacy rules
- **LLM Client Initialization**: All 5 providers (Mistral, Copilot, OpenAI, Anthropic, Azure)
- **Privacy Protections**: No customer data in prompts

### Manual Testing

1. **Test Unknown Intent:**
   ```bash
   curl -X POST http://localhost:5009/chat \
     -H "Content-Type: application/json" \
     -d '{
       "message": "I need something beautiful",
       "session_id": "test-123"
     }'
   ```

2. **Test Low Confidence:**
   ```bash
   curl -X POST http://localhost:5009/chat \
     -H "Content-Type: application/json" \
     -d '{
       "message": "tiles",
       "session_id": "test-123"
     }'
   ```

3. **Test Empty Results Retry:**
   ```bash
   curl -X POST http://localhost:5009/chat \
     -H "Content-Type: application/json" \
     -d '{
       "message": "show me xyz123 nonexistent product",
       "session_id": "test-123"
     }'
   ```

## Feature Flags

### Disable LLM Fallback Completely

```bash
LLM_FALLBACK_ENABLED=false
```

This will:
- Skip all LLM calls
- Use old disambiguation menu at Step 1.5
- Use static error messages for empty searches

### Disable Only Post-API Retry

```bash
LLM_RETRY_ON_EMPTY_RESULTS=false
```

This will:
- Keep Step 1.5 LLM fallback active
- Skip Step 3.8 retry on empty results

## Cost Estimation

The module tracks estimated costs per LLM call:

**Example Costs (default settings):**
- Input: 842 tokens × $0.002/1K = $0.00168
- Output: 156 tokens × $0.008/1K = $0.00125
- **Total: $0.00293 per call**

**Typical Usage:**
- 1000 messages/day with 20% LLM fallback = 200 calls
- Daily cost: 200 × $0.003 = **$0.60/day** = **$18/month**

## Security & Privacy

### What IS Sent to LLM

✅ Product names (from StoreLoader)
✅ Category names and slugs
✅ Attribute values (finishes, colors, sizes)
✅ Tag names
✅ User's chat message (sanitized)
✅ Last 3-5 session messages (sanitized)
✅ Available intent names

### What is NEVER Sent

❌ Customer ID, email, name
❌ Order history, order IDs
❌ WooCommerce API keys
❌ Payment information
❌ Customer addresses
❌ Any PII from user messages (stripped via `_sanitize_for_llm()`)

### PII Sanitization Examples

```python
Input:  "My email is john@example.com"
Output: "My email is [EMAIL]"

Input:  "Call me at 555-123-4567"
Output: "Call me at [PHONE]"

Input:  "Card: 1234-5678-9012-3456"
Output: "Card: [CARD]"
```

## Troubleshooting

### LLM Not Being Called

1. Check `LLM_FALLBACK_ENABLED=true`
2. Verify provider credentials are set
3. Check logs for trigger conditions

### LLM Returns Invalid JSON

- LLM sometimes wraps JSON in markdown code blocks
- Module automatically extracts JSON from ```json...``` blocks
- Check `llm_latency_ms` - timeout may be too short

### High Latency

- Increase `LLM_TIMEOUT_SECONDS`
- Use faster model (e.g., GPT-3.5 instead of GPT-4)
- Reduce `LLM_MAX_TOKENS`

### Cost Too High

- Set `LLM_RETRY_ON_EMPTY_RESULTS=false` to disable Step 3.8
- Use cheaper model
- Increase `LOW_CONFIDENCE_THRESHOLD` to trigger less often

## Best Practices

1. **Start with Mistral**: Default provider, good balance of quality and cost
2. **Monitor costs**: Check logs for `cost_estimate` field
3. **Tune confidence threshold**: Adjust `LOW_CONFIDENCE_THRESHOLD` based on your data
4. **Keep store data fresh**: StoreLoader auto-refreshes every 6 hours
5. **Test with real queries**: Use actual customer messages to validate
6. **Use feature flags**: Start with just Step 1.5, add Step 3.8 later

## Future Enhancements

Potential improvements:
- [ ] Token budget limits (daily/hourly caps)
- [ ] Caching for common queries
- [ ] Multi-turn conversation support
- [ ] A/B testing framework
- [ ] Fine-tuned models for tiles domain
- [ ] Streaming responses for better UX

## Support

For issues or questions:
- Check logs in `logs/YYYY-MM-DD/chat.txt`
- Review LLM API call metadata in response
- Verify environment variables are set correctly
- Test with `python -m pytest test_llm_fallback.py -v`
