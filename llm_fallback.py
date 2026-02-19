"""
LLM Fallback Module — Intelligent fallback when regex classifier fails.

Handles two scenarios:
1. Pre-API fallback (Step 1.5) — when classifier returns UNKNOWN or low confidence
2. Post-API fallback (Step 3.8) — when WooCommerce API returns 0 products

Privacy-First Design:
- Only sends public store catalog data (product names, categories, attributes)
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

logger = get_logger("miraq_chat")


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


def _build_store_context(store_loader) -> Dict[str, Any]:
    """
    Build public store context from StoreLoader.
    ONLY includes public catalog data — no customer PII.
    
    Args:
        store_loader: StoreLoader instance
        
    Returns:
        Dict with public store data
    """
    context = {
        "products": [],
        "categories": [],
        "attributes": {},
        "tags": [],
    }
    
    if not store_loader:
        return context
    
    # Product names (first 100 to keep prompt size reasonable)
    context["products"] = [
        p.get("name", "") for p in store_loader.products[:100]
        if p.get("name")
    ]
    
    # Category names
    context["categories"] = [
        {"id": c.get("id"), "name": c.get("name", ""), "slug": c.get("slug", "")}
        for c in store_loader.categories
        if c.get("name")
    ]
    
    # Attribute terms (finishes, sizes, colors, etc.)
    for attr in store_loader.attributes:
        attr_slug = attr.get("slug", "")
        if attr_slug:
            terms = store_loader.get_all_attribute_terms(attr_slug)
            if terms:
                context["attributes"][attr_slug] = [
                    t.get("name", "") for t in terms if t.get("name")
                ]
    
    # Tag names (first 50 for prompt size limit)
    context["tags"] = [
        {"id": t.get("id"), "name": t.get("name", ""), "slug": t.get("slug", "")}
        for t in store_loader.tags[:50]  # Limit to first 50 tags
        if t.get("name")
    ]
    
    return context


def _build_system_prompt(store_context: Dict[str, Any]) -> str:
    """
    Construct system prompt with store catalog data.
    
    Args:
        store_context: Public store data from _build_store_context()
        
    Returns:
        System prompt string
    """
    # Extract context
    products_sample = ", ".join(store_context.get("products", [])[:50])
    categories = ", ".join([c["name"] for c in store_context.get("categories", [])])
    
    # Build attribute descriptions
    attributes_desc = []
    for attr_slug, terms in store_context.get("attributes", {}).items():
        attr_name = attr_slug.replace("pa_", "").replace("-", " ").title()
        terms_sample = ", ".join(terms[:10])
        attributes_desc.append(f"- {attr_name}: {terms_sample}")
    
    attributes_text = "\n".join(attributes_desc) if attributes_desc else "None available"
    
    prompt = f"""You are an AI assistant for WGC Tiles Store, helping customers find tile products.

**Your task**: Interpret the user's message and return structured JSON to help route their query.

**Available Products** (sample): {products_sample}

**Categories**: {categories}

**Available Attributes**:
{attributes_text}

**Valid Intents**:
- product_search: User wants to find a specific product
- category_browse: User wants to browse a category
- filter_by_finish: User wants tiles by finish (matte, polished, etc.)
- filter_by_color: User wants tiles by color
- filter_by_size: User wants tiles by size
- filter_by_application: User wants tiles for specific use (floor, wall, etc.)
- order_inquiry: User asking about orders
- general_question: General question about products/services

**IMPORTANT Privacy Rules**:
- NEVER ask for or reference customer personal information
- NEVER mention customer IDs, emails, phone numbers, or addresses
- Only work with public product catalog information

**Response Format** (JSON only):
{{
  "intent": "product_search",
  "entities": {{
    "product_name": "Marble",
    "category_name": "Floor Tiles",
    "finish": "Polished",
    "color_tone": "White",
    "application": "Kitchen"
  }},
  "bot_message": "I found some beautiful marble tiles for your kitchen!",
  "confidence": 0.85,
  "fallback_type": "intent_resolved"
}}

**fallback_type options**:
- "intent_resolved": You identified a clear intent and entities
- "entity_extracted": You extracted additional entities to help with search
- "conversational": General Q&A response, no specific product intent

Return ONLY valid JSON. No markdown, no explanation, just the JSON object."""
    
    return prompt


# ══════════════════════════════════════════════════════════════
# LLM CLIENT (Multi-Provider Support)
# ══════════════════════════════════════════════════════════════

class LLMClient:
    """
    Abstraction over LLM providers — configurable via environment variables.
    
    Supported providers:
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
        
        # Initialize provider-specific settings
        if self.provider == "copilot":
            self.api_token = COPILOT_API_TOKEN
            self.api_url = LLM_API_BASE_URL or "https://api.githubcopilot.com/chat/completions"
        elif self.provider == "openai":
            self.api_key = LLM_API_KEY
            self.api_url = LLM_API_BASE_URL or "https://api.openai.com/v1/chat/completions"
        elif self.provider == "anthropic":
            self.api_key = LLM_API_KEY
            self.api_url = LLM_API_BASE_URL or "https://api.anthropic.com/v1/messages"
        elif self.provider == "azure_openai":
            self.api_key = LLM_API_KEY
            self.api_url = LLM_API_BASE_URL  # Must be provided for Azure
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")
    
    def chat_completion(
        self, 
        system_prompt: str, 
        user_message: str
    ) -> Dict[str, Any]:
        """
        Send a chat completion request to configured LLM provider.
        
        Args:
            system_prompt: System instructions
            user_message: User's message
            
        Returns:
            Dict with:
                - content: LLM response text
                - input_tokens: Input tokens used
                - output_tokens: Output tokens used
                - total_tokens: Total tokens used
                - model: Model used
                
        Raises:
            Exception: If API call fails
        """
        start_time = time.time()
        
        try:
            if self.provider in ["copilot", "openai", "azure_openai"]:
                result = self._openai_style_completion(system_prompt, user_message)
            elif self.provider == "anthropic":
                result = self._anthropic_completion(system_prompt, user_message)
            else:
                raise ValueError(f"Unsupported provider: {self.provider}")
            
            # Add latency
            result["latency_ms"] = int((time.time() - start_time) * 1000)
            
            return result
            
        except Exception as e:
            logger.error(f"LLM API call failed | provider={self.provider} | error={str(e)}")
            raise
    
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
) -> Dict[str, Any]:
    """
    LLM fallback when classifier fails (UNKNOWN intent, low confidence, or missing entities).
    
    Args:
        user_message: User's original message
        original_intent: Intent from regex classifier
        original_confidence: Confidence from regex classifier
        trigger_reason: Why LLM was triggered (unknown_intent, low_confidence, missing_entities)
        session_id: Session identifier
        store_loader: StoreLoader instance for catalog data
        session_history: Last few messages for context (optional)
        
    Returns:
        Dict with:
            - success: bool
            - fallback_type: str (intent_resolved, entity_extracted, conversational)
            - intent: str (if resolved)
            - entities: dict (if extracted)
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
        
        # Build store context
        store_context = _build_store_context(store_loader)
        
        # Build system prompt
        system_prompt = _build_system_prompt(store_context)
        
        # Add session history context if available
        context_messages = []
        if session_history:
            recent = session_history[-3:]  # Last 3 messages
            for msg in recent:
                role = msg.get("role", "user")
                content = _sanitize_for_llm(msg.get("message", ""))
                context_messages.append(f"{role}: {content}")
        
        # Construct user prompt
        user_prompt = sanitized_message
        if context_messages:
            user_prompt = f"Context:\n" + "\n".join(context_messages) + f"\n\nCurrent: {sanitized_message}"
        
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
        llm_content = llm_response["content"].strip()
        
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', llm_content, re.DOTALL)
        if json_match:
            llm_content = json_match.group(1)
        
        # Parse JSON
        try:
            parsed = json.loads(llm_content)
        except json.JSONDecodeError as e:
            logger.warning(f"Step 1.5: Failed to parse LLM response as JSON | error={str(e)}")
            return {
                "success": False,
                "error": "LLM returned invalid JSON",
                "fallback_type": "error",
            }
        
        # Extract fields
        fallback_type = parsed.get("fallback_type", "conversational")
        resolved_intent = parsed.get("intent", "unknown")
        resolved_entities = parsed.get("entities", {})
        bot_message = parsed.get("bot_message", "")
        new_confidence = parsed.get("confidence", 0.70)
        
        # Log resolution
        logger.info(
            f"Step 1.5: LLM fallback resolved | fallback_type={fallback_type} | "
            f"resolved_intent={resolved_intent} | "
            f"resolved_entities={resolved_entities} | "
            f"new_confidence={new_confidence:.2f}"
        )
        
        # Return result
        return {
            "success": True,
            "fallback_type": fallback_type,
            "intent": resolved_intent,
            "entities": resolved_entities,
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
    
    Suggests:
    - Spelling corrections
    - Broader search terms
    - Alternative categories
    
    Args:
        user_message: User's original message
        original_intent: Intent from classifier
        entities: Extracted entities
        session_id: Session identifier
        store_loader: StoreLoader instance
        
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
        
        # Build store context
        store_context = _build_store_context(store_loader)
        
        # Build specialized system prompt for empty results
        products_sample = ", ".join(store_context.get("products", [])[:50])
        categories = ", ".join([c["name"] for c in store_context.get("categories", [])])
        
        system_prompt = f"""You are an AI assistant helping customers find products when their search returned no results.

**Available Products** (sample): {products_sample}

**Categories**: {categories}

**User searched for**: {sanitized_message}

**Task**: Suggest a corrected search term OR provide a helpful alternative.

**Response Format** (JSON only):
{{
  "retry_type": "corrected_search",
  "corrected_term": "Marble",
  "suggestion_message": "Did you mean 'Marble'? Let me search for that."
}}

OR

{{
  "retry_type": "suggestion",
  "suggestion_message": "I couldn't find that specific product. Here are some similar options you might like: [list alternatives]"
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
        llm_content = llm_response["content"].strip()
        
        # Extract JSON
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', llm_content, re.DOTALL)
        if json_match:
            llm_content = json_match.group(1)
        
        try:
            parsed = json.loads(llm_content)
        except json.JSONDecodeError:
            logger.warning("Step 3.8: Failed to parse LLM response as JSON")
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
