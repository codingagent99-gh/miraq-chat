"""
LLM Classifier (Layer 3)
LLM as binary judge for intent classification
Only classifies - does NOT generate answers

UPDATED: Now works with ANY LLM provider (OpenAI, Mistral Local, Mistral Cloud)
"""

import json
import os
import re
import logging
from typing import Optional, Dict, Any, List

from .intent_types import IntentType, ClassificationResult

logger = logging.getLogger(__name__)

# LLM Prompts for Intent Classification
SYSTEM_PROMPT = """You are an intent classifier. Your task is to classify user queries into one of three categories:
1. PERSONA - Questions about the assistant itself (greetings, identity, capabilities, wellbeing)
2. DOCUMENT - Questions that require searching through documents/knowledge base
3. OUT_OF_SCOPE - Questions that are completely unrelated (math, weather, jokes, personal advice, etc.)

Respond ONLY with valid JSON in this exact format:
{
  "intent": "PERSONA|DOCUMENT|OUT_OF_SCOPE",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}

Examples:
- "hello" or "hi" → PERSONA (greeting)
- "who are you" → PERSONA (bot identity)
- "what can you do" → PERSONA (capabilities)
- "what is the refund policy" → DOCUMENT
- "tell me a joke" → OUT_OF_SCOPE
- "what's 5+5" → OUT_OF_SCOPE"""

USER_PROMPT_TEMPLATE = """Classify this query: "{query}"

Return only JSON, no other text."""


class LLMClassifier:
    """
    Layer 3 classifier using LLM as binary judge.
    UPDATED: Works with ANY LLM provider (OpenAI, Mistral Local, Mistral Cloud)
    """
    
    def __init__(self, llm_provider=None, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the LLM classifier.
        
        Args:
            llm_provider: LLM provider instance (any BaseLLMProvider implementation)
            config: Optional configuration dictionary. If None, loads defaults.
        """
        self.llm = llm_provider
        self._load_config(config)
        
        if llm_provider:
            provider_name = getattr(llm_provider, 'provider_name', 'unknown')
            logger.info("LLM classifier initialized with provider: %s", provider_name)
        else:
            logger.warning("LLM classifier initialized without provider - will be skipped")

    def _load_config(self, config: Optional[Dict[str, Any]]):
        """Load configuration from dict or defaults"""
        if config:
            self.confidence_threshold = config.get("thresholds", {}).get("layer3_llm_confidence", 0.75)
            self.out_of_scope_keywords = config.get("layer3_llm_intent_keywords", {}).get("out_of_scope_keywords", [])
        else:
            # Fallback defaults
            self.confidence_threshold = 0.75
            self.out_of_scope_keywords = ["NUMERIC", "CALCULATION", "MATH", "JOKE", "WEATHER"]
    
    def _parse_intent(self, intent_str: str) -> IntentType:
        """Parse intent string to IntentType enum"""
        intent_upper = intent_str.upper().strip()
        
        # Handle malformed pipe-separated intents
        if '|' in intent_upper:
            logger.warning("LLM returned pipe-separated intents: '%s'", intent_str)
            parts = [p.strip() for p in intent_upper.split('|')]
            if "OUT_OF_SCOPE" in parts:
                logger.info("Selecting OUT_OF_SCOPE from multiple intents")
                return IntentType.OUT_OF_SCOPE
            for part in parts:
                if part == "PERSONA":
                    return IntentType.PERSONA
                elif part == "DOCUMENT":
                    return IntentType.DOCUMENT
                elif part == "OUT_OF_SCOPE":
                    return IntentType.OUT_OF_SCOPE
        
        # Standard parsing
        if intent_upper == "PERSONA":
            return IntentType.PERSONA
        elif intent_upper == "DOCUMENT":
            return IntentType.DOCUMENT
        elif intent_upper == "OUT_OF_SCOPE":
            return IntentType.OUT_OF_SCOPE
        else:
            # Map custom intents to OUT_OF_SCOPE
            for keyword in self.out_of_scope_keywords:
                if keyword in intent_upper:
                    logger.info("Mapped custom intent '%s' to OUT_OF_SCOPE", intent_str)
                    return IntentType.OUT_OF_SCOPE
            
            logger.warning("Unknown intent '%s', defaulting to DOCUMENT", intent_str)
            return IntentType.DOCUMENT
    
    def _call_llm(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Call LLM provider for classification.
        UPDATED: Uses standardized generate_answer method (works with ALL providers)
        """
        if not self.llm:
            return None
        
        user_prompt = USER_PROMPT_TEMPLATE.format(query=query)
        
        try:
            provider_name = getattr(self.llm, 'provider_name', 'unknown')
            logger.debug(f"Using provider: {provider_name}")
            
            # Use standardized generate_answer method
            # This works with OpenAI, Mistral Local, and Mistral Cloud
            result = self.llm.generate_answer(
                system_message=SYSTEM_PROMPT,
                user_message=user_prompt,
                temperature=0.1,
                max_tokens=200
            )
            
            if result and result.get('success'):
                return {
                    'content': result.get('answer', ''),
                    'tokens_used': result.get('tokens_used', 0)
                }
            else:
                logger.warning(f"LLM call failed: {result}")
                return None
            
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _parse_response(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse LLM response JSON - extracts only the FIRST complete JSON object"""
        if not content:
            return None
        
        try:
            # Clean up response
            content = content.strip()
            
            # Remove markdown code blocks if present
            content = re.sub(r'^```json\s*|\s*```$', '', content, flags=re.MULTILINE)
            content = re.sub(r'^```\s*|\s*```$', '', content, flags=re.MULTILINE)
            
            # Extract ONLY the first complete JSON object
            first_json = self._extract_first_json(content)
            
            if first_json:
                return json.loads(first_json)
            
            # Fallback: try simple line-by-line parsing
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.strip().startswith('{'):
                    try:
                        return json.loads(line.strip())
                    except:
                        pass
            
            # Final fallback: try full content
            return json.loads(content)
            
        except json.JSONDecodeError as e:
            logger.error("Failed to parse LLM response: %s", e)
            print(f"Layer 3: Failed to parse LLM response")
            logger.debug("Raw content: %s", content[:200])
            return None
    
    def _extract_first_json(self, text: str) -> Optional[str]:
        """Extract the first complete JSON object from text using bracket counting"""
        start = text.find('{')
        if start == -1:
            return None
        
        depth = 0
        in_string = False
        escape_next = False
        
        for i, char in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            
            if char == '\\' and in_string:
                escape_next = True
                continue
            
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            
            if in_string:
                continue
            
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i+1]
        
        return None
    
    def classify(self, query: str) -> Optional[ClassificationResult]:
        """
        Classify query using LLM as binary judge.
        UPDATED: Works with any LLM provider
        
        Args:
            query: The user's query string
            
        Returns:
            ClassificationResult with intent classification
        """
        if not query or not query.strip():
            return None
        
        if not self.llm:
            logger.debug("Layer 3: Skipped - LLM provider not available")
            return None
        
        try:
            # Call LLM using standardized method
            llm_result = self._call_llm(query.strip())
            
            if not llm_result:
                logger.warning("Layer 3: LLM call returned no result")
                return None
            
            # Parse response
            parsed = self._parse_response(llm_result.get('content', ''))
            
            if not parsed:
                logger.warning("Layer 3: Failed to parse LLM response")
                return None
            
            # Extract fields
            intent_str = parsed.get('intent', 'DOCUMENT')
            confidence = float(parsed.get('confidence', 0.5))
            reasoning = parsed.get('reasoning', 'LLM classification')
            tokens_used = llm_result.get('tokens_used', 0)
            
            # Parse intent
            intent = self._parse_intent(intent_str)
            
            provider_name = getattr(self.llm, 'provider_name', 'unknown')
            
            logger.info(
                "Layer 3 (%s): intent=%s, confidence=%.2f, tokens=%d",
                provider_name, intent.value, confidence, tokens_used
            )
            
            return ClassificationResult(
                intent=intent,
                confidence=confidence,
                layer_used=3,
                subtype=None,
                reasoning=reasoning,
                llm_used=True,
                tokens_used=tokens_used,
                metadata={
                    "classification_method": "llm_classifier",
                    "llm_provider": provider_name,
                    "llm_intent": intent_str,
                    "llm_reasoning": reasoning
                }
            )
            
        except Exception as e:
            logger.error("Layer 3 classification error: %s", e)
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get classifier statistics"""
        provider_name = None
        if self.llm:
            provider_name = getattr(self.llm, 'provider_name', 'unknown')
        
        return {
            "has_provider": self.llm is not None,
            "provider_name": provider_name
        }