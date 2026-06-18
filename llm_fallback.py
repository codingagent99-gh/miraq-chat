"""
LLM Fallback Module — Intelligent fallback when regex classifier fails.

Handles two scenarios:
1. Pre-API fallback (Step 1.5) — when classifier returns UNKNOWN or low confidence
2. Post-API fallback (Step 3.8) — when WooCommerce API returns 0 products

Privacy-First Design:
- Sends only what the local classifier already resolved (intent, confidence, entities)
- No store catalog data (product names, categories, attributes, tags)
- Sanitizes user messages to remove PII (emails, phone numbers)
- Never sends customer IDs, order history, or payment information
"""

import re
import json
import time
import requests
from typing import Dict, List, Optional, Any, Tuple
from chat_logger import get_logger, sanitize_log_string
from app_config import (
    LLM_PROVIDER,
    LLM_MODEL,
    LLM_API_KEY,
    LLM_API_BASE_URL,
    COPILOT_API_TOKEN,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_TIMEOUT_SECONDS,
    LLM_COST_PER_1K_INPUT,
    LLM_COST_PER_1K_OUTPUT,
)
from models import Intent

logger = get_logger("miraq_chat")

# Closed set of valid intent values derived from the Intent enum.
# Used in the LLM system prompt so it can only pick from this fixed list.
_VALID_INTENTS = ", ".join(i.value for i in Intent if i != Intent.UNKNOWN) + ", unknown"


# ══════════════════════════════════════════════════════════════
# PRIVACY & SANITIZATION
# ══════════════════════════════════════════════════════════════

def _sanitize_for_llm(text: str) -> str:
    """
    Remove PII from user messages before sending to LLM.
    
    Strips:
    - Email addresses
    - Phone numbers
    - Credit card numbers
    - Other sensitive patterns
    
    Args:
        text: User message text
        
    Returns:
        Sanitized text safe to send to LLM
    """
    if not text:
        return text
    
    # Remove email addresses
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]', text)

    # Remove phone numbers - use most specific pattern first to avoid overlaps
    # International format with country code
    text = re.sub(r'\b\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}\b', '[PHONE]', text)
    # Standard US format
    text = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[PHONE]', text)

    # Remove credit card numbers (basic pattern)
    text = re.sub(r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b', '[CARD]', text)

    # Remove SSN-like patterns
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[SSN]', text)

    return text


def _extract_json_object(text: str) -> str:
    """
    Robustly extract the first JSON object {...} from an LLM response.

    Handles all common LLM response formats:
    - Clean JSON                       → returned as-is
    - ```json\\n{...}\\n```              → fences stripped
    - ```\\n{...}\\n```                  → fences stripped (no language tag)
    - Prose before/after the JSON      → everything outside {...} discarded

    If no braces are found the original text is returned unchanged so that
    the downstream json.loads call produces a clear, attributable error.
    """
    # Strip all markdown code fence variants (with or without language tag)
    text = re.sub(r'```(?:json)?\s*', '', text)
    text = re.sub(r'```', '', text)
    text = text.strip()

    # Extract the first complete {...} block, ignoring any leading/trailing prose
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]

    # No braces found — return as-is so json.loads raises a meaningful error
    return text


_JSON_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')


def _escape_control_chars_in_strings(text: str) -> str:
    """
    Some LLMs emit literal newline/tab characters inside JSON string values
    (e.g. a multi-line bullet-point suggestion) instead of escaping them as
    \\n / \\t. json.loads rejects unescaped control characters inside a
    string per the JSON spec, so this finds each quoted string literal and
    escapes any raw control characters found strictly within it, leaving
    insignificant whitespace BETWEEN tokens (which is legal as literal
    newlines) untouched.
    """
    def _fix(match: "re.Match") -> str:
        inner = match.group(0)[1:-1]
        inner = (
            inner.replace('\r\n', '\\n')
                 .replace('\n', '\\n')
                 .replace('\r', '\\n')
                 .replace('\t', '\\t')
        )
        return '"' + inner + '"'

    return _JSON_STRING_RE.sub(_fix, text)


# ══════════════════════════════════════════════════════════════
# RETRY HELPERS
# ══════════════════════════════════════════════════════════════

def _is_retryable_error(exc: Exception) -> bool:
    """
    Returns True for transient errors that are safe and worth retrying once.

    Retries on:
    - Timeout / connection-level failures  (network blip)
    - HTTP 429 Too Many Requests           (rate limit — provider will accept soon)
    - HTTP 502 / 503 / 504                 (provider-side transient overload)

    Does NOT retry on:
    - HTTP 400 Bad Request  — our payload is malformed; retrying won't help
    - HTTP 401 / 403        — auth error; retrying wastes quota and money
    - HTTP 404              — wrong endpoint; retrying won't help
    - ValueError            — code/config bug; retrying won't help
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status in (429, 502, 503, 504)
    return False


def _build_classifier_context(
    original_intent: str,
    original_confidence: float,
    entities_summary: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Build a lean context from what the classifier already resolved.
    No store catalog data — just the classifier's output.

    Args:
        original_intent: Intent string returned by the local classifier
        original_confidence: Confidence score from the local classifier
        entities_summary: Compact dict of non-None entities the classifier extracted

    Returns:
        Dict with classifier intent, confidence, and resolved entities
    """
    return {
        "classifier_intent": original_intent,
        "classifier_confidence": original_confidence,
        "resolved_entities": entities_summary or {},
    }


def _build_system_prompt(
    classifier_context: Dict[str, Any],
    session_history: Optional[List[Dict]] = None,
) -> str:
    """
    Construct a lean intent-only system prompt.
    """
    valid_intents = _VALID_INTENTS

    classifier_intent = classifier_context.get("classifier_intent", "unknown")
    classifier_confidence = classifier_context.get("classifier_confidence", 0.0)
    resolved_entities = classifier_context.get("resolved_entities", {})
    entities_str = json.dumps(resolved_entities) if resolved_entities else "none"

    history_lines = []
    if session_history:
        for msg in session_history[-3:]:
            role = msg.get("role", "user")
            content = _sanitize_for_llm(msg.get("message", ""))
            history_lines.append(f"{role}: {content}")
    history_text = "\n".join(history_lines) if history_lines else "none"

    prompt = f"""You are an intent classifier for a WooCommerce store chatbot.

Your ONLY job is to determine the user's intent from their message.
Do NOT extract entities — that is already handled by the local classifier.

**Valid intents** (pick exactly one):
{valid_intents}

**Important disambiguation rules:**
- If the user asks to check if a specific product comes in a certain attribute (e.g., "Do you have 3x3 for Ansel?", "Does the subway tile come in matte?"), use "product_search".
- If the user asks for a general list of options without specifying what they want (e.g., "What sizes does Ansel come in?", "Show me the colors for this tile"), use "product_attribute_info".

**Classifier context** (what the local classifier already resolved):
- Intent: {classifier_intent} (confidence: {classifier_confidence:.2f})
- Resolved entities: {entities_str}

**Conversation history** (last 3 turns):
{history_text}

Return ONLY valid JSON:
{{
  "intent": "product_search",
  "confidence": 0.90,
  "fallback_type": "intent_resolved"
}}

If the user's intent is genuinely unclear even with context, return:
{{
  "intent": "unknown",
  "confidence": 0.0,
  "fallback_type": "conversational",
  "bot_message": "I wasn't sure what you were looking for. Did you mean:\\n• **Browse products** in a specific category\\n• **Check your order status**\\n• **Search for a specific product**"
}}

Return ONLY valid JSON. No markdown, no explanation, just the JSON object."""

    return prompt


# ══════════════════════════════════════════════════════════════
# LLM CLIENT (Multi-Provider Support)
# ══════════════════════════════════════════════════════════════

class LLMClient:
    """
    Abstraction over LLM providers — configurable via environment variables.
    
    Supported providers:
    - mistral: Mistral AI Cloud API
    - copilot: GitHub Copilot API
    - openai: OpenAI API
    - anthropic: Anthropic Claude API
    - azure_openai: Azure OpenAI Service
    """
    
    def __init__(self):
        self.provider = LLM_PROVIDER.lower()
        self.model = LLM_MODEL
        self.temperature = LLM_TEMPERATURE
        self.max_tokens = LLM_MAX_TOKENS
        self.timeout = LLM_TIMEOUT_SECONDS
        
        # ─── URL CLEANER ───
        # Clean up the base URL right away in case .env has markdown links like [url](url)
        clean_base_url = LLM_API_BASE_URL
        if clean_base_url and isinstance(clean_base_url, str):
            # Extracts just the http://... part out of [text](http://...)
            match = re.search(r'\]\((https?://.*?)\)', clean_base_url)
            if match:
                clean_base_url = match.group(1)
            # Strips out any stray angle brackets < > or spaces
            clean_base_url = clean_base_url.strip('<> ')
        
        # Initialize provider-specific settings
        if self.provider == "copilot":
            self.api_token = COPILOT_API_TOKEN
            self.api_url = clean_base_url or "https://api.githubcopilot.com/chat/completions"
        elif self.provider == "openai":
            self.api_key = LLM_API_KEY
            self.api_url = clean_base_url or "https://api.openai.com/v1/chat/completions"
        elif self.provider == "anthropic":
            self.api_key = LLM_API_KEY
            self.api_url = clean_base_url or "https://api.anthropic.com/v1/messages"
        elif self.provider == "azure_openai":
            self.api_key = LLM_API_KEY
            self.api_url = clean_base_url  # Must be provided for Azure
        elif self.provider == "mistral":
            self.api_key = LLM_API_KEY
            self.api_url = clean_base_url or "https://api.mistral.ai/v1/chat/completions"
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")
    
    def chat_completion(
        self,
        system_prompt: str,
        user_message: str
    ) -> Dict[str, Any]:
        """
        Send a chat completion request to the configured LLM provider.

        Includes a single retry for transient network/provider errors (timeout,
        connection reset, HTTP 429/502/503/504). Non-retryable errors (auth,
        bad request, config bugs) are re-raised immediately to avoid burning
        tokens on calls that will never succeed.

        Args:
            system_prompt: System instructions
            user_message: User's message

        Returns:
            Dict with content, input_tokens, output_tokens, total_tokens, model, latency_ms

        Raises:
            Exception: If the API call fails after the retry budget is exhausted
        """
        start_time = time.time()
        last_exc: Optional[Exception] = None

        for attempt in range(2):  # attempt 0 = initial call, attempt 1 = one retry
            try:
                if attempt > 0:
                    time.sleep(1)  # brief pause before retry
                    logger.warning(
                        f"LLM retry attempt {attempt} | provider={self.provider} | "
                        f"previous_error={last_exc}"
                    )

                if self.provider in ["copilot", "openai", "azure_openai", "mistral"]:
                    result = self._openai_style_completion(system_prompt, user_message)
                elif self.provider == "anthropic":
                    result = self._anthropic_completion(system_prompt, user_message)
                else:
                    raise ValueError(f"Unsupported provider: {self.provider}")

                result["latency_ms"] = int((time.time() - start_time) * 1000)
                return result

            except Exception as e:
                last_exc = e
                if not _is_retryable_error(e) or attempt == 1:
                    # Non-retryable error, or retry budget exhausted — give up
                    logger.error(
                        f"LLM API call failed | provider={self.provider} | "
                        f"attempt={attempt} | error={str(e)}"
                    )
                    raise
                logger.warning(
                    f"LLM transient error, will retry | provider={self.provider} | "
                    f"error={str(e)}"
                )

        raise last_exc  # unreachable, satisfies type checkers
    
    def _openai_style_completion(
        self, 
        system_prompt: str, 
        user_message: str
    ) -> Dict[str, Any]:
        """
        OpenAI-compatible API call (works for OpenAI, Azure OpenAI, Copilot).
        """
        headers = {
            "Content-Type": "application/json",
        }
        
        # Provider-specific auth
        if self.provider == "copilot":
            headers["Authorization"] = f"Bearer {self.api_token}"
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        
        response = requests.post(
            self.api_url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Extract response
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        
        return {
            "content": content,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "model": self.model,
        }
    
    def _anthropic_completion(
        self, 
        system_prompt: str, 
        user_message: str
    ) -> Dict[str, Any]:
        """
        Anthropic Claude API call.
        """
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        
        payload = {
            "model": self.model,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        
        response = requests.post(
            self.api_url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Extract response
        content = data["content"][0]["text"]
        usage = data.get("usage", {})
        
        return {
            "content": content,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "model": self.model,
        }


# ══════════════════════════════════════════════════════════════
# STEP 1.5: PRE-API FALLBACK
# ══════════════════════════════════════════════════════════════

def llm_fallback(
    user_message: str,
    original_intent: str,
    original_confidence: float,
    trigger_reason: str,
    session_id: str,
    store_loader,
    session_history: Optional[List[Dict]] = None,
    entities_summary: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    LLM fallback when classifier fails (UNKNOWN intent, low confidence, or missing entities).

    The LLM performs intent-only classification. Entity extraction remains with the
    local classifier. No store catalog data is sent to the LLM.

    Args:
        user_message: User's original message
        original_intent: Intent from regex classifier
        original_confidence: Confidence from regex classifier
        trigger_reason: Why LLM was triggered (unknown_intent, low_confidence, missing_entities)
        session_id: Session identifier
        store_loader: StoreLoader instance (kept for backward compatibility, not used for prompts)
        session_history: Last few messages for context (optional)
        entities_summary: Compact dict of non-None entities from classifier (optional)

    Returns:
        Dict with:
            - success: bool
            - fallback_type: str (intent_resolved, conversational)
            - intent: str (if resolved)
            - bot_message: str
            - confidence: float
            - metadata: dict with LLM call details
    """
    # Log trigger
    logger.info(
        f"Step 1.5: LLM fallback triggered | session={session_id} | "
        f"reason={trigger_reason} | original_intent={original_intent} | "
        f"confidence={original_confidence:.2f} | message=\"{sanitize_log_string(user_message)}\""
    )

    try:
        # Sanitize user message
        sanitized_message = _sanitize_for_llm(user_message)

        # Build lean classifier context (no store catalog data)
        classifier_context = _build_classifier_context(
            original_intent, original_confidence, entities_summary
        )

        # Build system prompt with classifier context and conversation history
        system_prompt = _build_system_prompt(classifier_context, session_history)

        # User prompt is the sanitized message only
        user_prompt = sanitized_message
        
        # Call LLM
        llm_client = LLMClient()
        llm_response = llm_client.chat_completion(system_prompt, user_prompt)
        
        # Calculate cost
        input_cost = (llm_response["input_tokens"] / 1000) * LLM_COST_PER_1K_INPUT
        output_cost = (llm_response["output_tokens"] / 1000) * LLM_COST_PER_1K_OUTPUT
        total_cost = input_cost + output_cost
        
        # Log API call details
        logger.info(
            f"Step 1.5: LLM API call | model={llm_response['model']} | "
            f"input_tokens={llm_response['input_tokens']} | "
            f"output_tokens={llm_response['output_tokens']} | "
            f"total_tokens={llm_response['total_tokens']} | "
            f"latency_ms={llm_response['latency_ms']} | "
            f"cost_estimate=${total_cost:.4f}"
        )
        
        # Parse LLM response
        llm_content = _extract_json_object(llm_response["content"].strip())
        llm_content = _escape_control_chars_in_strings(llm_content)

        # Parse JSON
        try:
            parsed = json.loads(llm_content)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Step 1.5: Failed to parse LLM response as JSON | "
                f"error={str(e)} | raw_content={llm_response['content'][:200]!r}"
            )
            return {
                "success": False,
                "error": "LLM returned invalid JSON",
                "fallback_type": "error",
            }
        
        # Extract fields
        fallback_type = parsed.get("fallback_type", "conversational")
        resolved_intent = parsed.get("intent", "unknown")
        bot_message = parsed.get("bot_message", "")
        new_confidence = parsed.get("confidence", 0.70)

        # Log resolution
        logger.info(
            f"Step 1.5: LLM fallback resolved | fallback_type={fallback_type} | "
            f"resolved_intent={resolved_intent} | "
            f"new_confidence={new_confidence:.2f}"
        )

        # Return result — no entities; entity extraction stays with the local classifier
        return {
            "success": True,
            "fallback_type": fallback_type,
            "intent": resolved_intent,
            "bot_message": bot_message,
            "confidence": new_confidence,
            "metadata": {
                "llm_model": llm_response["model"],
                "llm_tokens_used": llm_response["total_tokens"],
                "llm_input_tokens": llm_response["input_tokens"],
                "llm_output_tokens": llm_response["output_tokens"],
                "llm_latency_ms": llm_response["latency_ms"],
                "llm_cost_estimate": round(total_cost, 4),
                "llm_trigger_reason": trigger_reason,  # Why LLM was called
                "original_intent": original_intent,
                "original_confidence": original_confidence,
                "provider": "llm_fallback",
            },
        }
        
    except Exception as e:
        logger.error(f"Step 1.5: LLM fallback failed | error={str(e)}")
        return {
            "success": False,
            "error": str(e),
            "fallback_type": "error",
        }


# ══════════════════════════════════════════════════════════════
# STEP 3.8: POST-API FALLBACK (Empty Search Results)
# ══════════════════════════════════════════════════════════════

def llm_retry_search(
    user_message: str,
    original_intent: str,
    entities: Dict[str, Any],
    session_id: str,
    store_loader,
) -> Dict[str, Any]:
    """
    LLM retry when WooCommerce API returns 0 products for a search.

    Suggests spelling corrections or broader search terms based on the user's
    search text. No store catalog data is sent to the LLM.

    Args:
        user_message: User's original message
        original_intent: Intent from classifier
        entities: Extracted entities (used for logging context)
        session_id: Session identifier
        store_loader: StoreLoader instance (kept for backward compatibility, not used)

    Returns:
        Dict with:
            - success: bool
            - retry_type: str (corrected_search, suggestion)
            - corrected_term: str (if corrected_search)
            - suggestion_message: str (if suggestion)
            - metadata: dict with LLM call details
    """
    # Log trigger
    logger.info(
        f"Step 3.8: LLM retry triggered | session={session_id} | "
        f"reason=empty_search_results | original_intent={original_intent} | "
        f"entities={entities} | message=\"{sanitize_log_string(user_message)}\""
    )

    try:
        # Sanitize user message
        sanitized_message = _sanitize_for_llm(user_message)

        # Build specialized system prompt for empty results — no store catalog data.
        # The LLM can suggest "Did you mean X?" based on the user's text alone.
        system_prompt = f"""You are an AI assistant helping customers when their product search returned no results.

**User searched for**: {sanitized_message}
**Search intent**: {original_intent}

**Task**: Based only on the user's search text, suggest a spelling correction OR provide helpful guidance. Do NOT invent product names or categories — rely only on the user's own words.

**Response Format** (JSON only):
{{
  "retry_type": "corrected_search",
  "corrected_term": "Marble",
  "suggestion_message": "Did you mean 'Marble'? Let me search for that."
}}

OR

{{
  "retry_type": "suggestion",
  "suggestion_message": "I couldn't find that specific product. You can try:\n\u2022 Browsing by category\n\u2022 Using a broader search term\n\u2022 Checking if the product name is spelled correctly"
}}

Return ONLY valid JSON."""

        # Call LLM
        llm_client = LLMClient()
        llm_response = llm_client.chat_completion(system_prompt, sanitized_message)

        # Calculate cost
        input_cost = (llm_response["input_tokens"] / 1000) * LLM_COST_PER_1K_INPUT
        output_cost = (llm_response["output_tokens"] / 1000) * LLM_COST_PER_1K_OUTPUT
        total_cost = input_cost + output_cost

        # Log API call
        logger.info(
            f"Step 3.8: LLM API call | model={llm_response['model']} | "
            f"input_tokens={llm_response['input_tokens']} | "
            f"output_tokens={llm_response['output_tokens']} | "
            f"total_tokens={llm_response['total_tokens']} | "
            f"latency_ms={llm_response['latency_ms']} | "
            f"cost_estimate=${total_cost:.4f}"
        )
        
        # Parse response
        llm_content = _extract_json_object(llm_response["content"].strip())
        llm_content = _escape_control_chars_in_strings(llm_content)

        try:
            parsed = json.loads(llm_content)
        except json.JSONDecodeError:
            logger.warning(
                f"Step 3.8: Failed to parse LLM response as JSON | "
                f"raw_content={llm_response['content'][:200]!r}"
            )
            return {
                "success": False,
                "error": "LLM returned invalid JSON",
            }
        
        # Extract fields
        retry_type = parsed.get("retry_type", "suggestion")
        corrected_term = parsed.get("corrected_term", "")
        suggestion_message = parsed.get("suggestion_message", "")
        
        logger.info(
            f"Step 3.8: LLM retry resolved | retry_type={retry_type} | "
            f"corrected_term={corrected_term}"
        )
        
        return {
            "success": True,
            "retry_type": retry_type,
            "corrected_term": corrected_term,
            "suggestion_message": suggestion_message,
            "metadata": {
                "llm_model": llm_response["model"],
                "llm_tokens_used": llm_response["total_tokens"],
                "llm_latency_ms": llm_response["latency_ms"],
                "llm_cost_estimate": round(total_cost, 4),
                "llm_trigger_reason": "empty_search_results",
            },
        }
        
    except Exception as e:
        logger.error(f"Step 3.8: LLM retry failed | error={str(e)}")
        return {
            "success": False,
            "error": str(e),
        }