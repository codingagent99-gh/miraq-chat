"""
Intent Router
Main orchestrator that combines all 3 classification layers
"""

import logging
from typing import Dict, Any, Optional

from .intent_types import IntentType, ClassificationResult
from .rule_based_classifier import RuleBasedClassifier
from .nlp_classifier import NLPClassifier
from .llm_classifier import LLMClassifier
from .persona_responses import get_persona_response, OUT_OF_SCOPE_RESPONSE

logger = logging.getLogger(__name__)


class IntentRouter:
    """
    Main intent router that orchestrates all 3 classification layers.
    
    Flow:
    1. Layer 1 (Rule-based) - Fast regex matching
    2. Layer 2 (NLP) - Embedding similarity (if available)
    3. Default - Proceed to document search
    4. Layer 3 (LLM) - Binary classification 
    """
    
    def __init__(
        self, 
        embeddings=None, 
        llm_provider=None,
        enable_layer2: bool = True,
        enable_layer3: bool = True
    ):
        """
        Initialize the intent router.
        
        Args:
            embeddings: LangChain embeddings for Layer 2 (optional)
            llm_provider: LLM provider for Layer 3 (optional)
            enable_layer2: Whether to use NLP classifier
            enable_layer3: Whether to use LLM classifier
        """
        # Layer 1 is always enabled
        self.layer1 = RuleBasedClassifier()
        
        # Layer 2 (optional)
        self.layer2 = None
        if enable_layer2 and embeddings:
            self.layer2 = NLPClassifier(embeddings)
        
        # Layer 3 (optional)
        self.layer3 = None
        if enable_layer3 and llm_provider:
            self.layer3 = LLMClassifier(llm_provider)
        
        logger.info(
            "IntentRouter initialized: Layer1=enabled, Layer2=%s, Layer3=%s",
            "enabled" if self.layer2 else "disabled",
            "enabled" if self.layer3 else "disabled"
        )
    
    def _build_persona_response(self, result: ClassificationResult) -> Dict[str, Any]:
        """Build response for persona intent"""
        subtype = result.subtype or "greeting"
        answer = get_persona_response(subtype)
        
        return {
            "intent": IntentType.PERSONA,
            "should_search_documents": False,
            "answer": answer,
            "classification": result,
            "metadata": {
                **result.metadata,
                "subtype": subtype,
                "layer_used": result.layer_used,
                "llm_used": result.llm_used,
                "tokens_used": result.tokens_used
            }
        }
    
    def _build_out_of_scope_response(self, result: ClassificationResult) -> Dict[str, Any]:
        """Build response for out-of-scope intent"""
        return {
            "intent": IntentType.OUT_OF_SCOPE,
            "should_search_documents": False,
            "answer": OUT_OF_SCOPE_RESPONSE,
            "classification": result,
            "metadata": {
                **result.metadata,
                "layer_used": result.layer_used,
                "llm_used": result.llm_used,
                "tokens_used": result.tokens_used
            }
        }
    
    def _build_document_response(self, result: Optional[ClassificationResult] = None) -> Dict[str, Any]:
        """Build response for document intent (pass to RAG)"""
        metadata = {}
        if result:
            metadata = {
                **result.metadata,
                "layer_used": result.layer_used,
                "llm_used": result.llm_used,
                "tokens_used": result.tokens_used
            }
        
        return {
            "intent": IntentType.DOCUMENT,
            "should_search_documents": True,
            "answer": None,
            "classification": result,
            "metadata": metadata
        }
    
    def route(self, query: str) -> Dict[str, Any]:
        """
        Route query through classification layers.
        
        Args:
            query: The user's query string
            
        Returns:
            Dictionary with:
                - intent: IntentType
                - should_search_documents: bool
                - answer: str (if persona/out_of_scope)
                - classification: ClassificationResult
                - metadata: dict
        """
        if not query or not query.strip():
            print("  [Router] Empty query, proceeding to document search")
            return self._build_document_response()
        
        query = query.strip()
        
        # ========== Layer 1: Rule-based ==========
        print("  [Router] Trying Layer 1 (rule-based)...")
        result = self.layer1.classify(query)
        
        if result:
            print(f"  [Router] Layer 1 MATCHED: intent={result.intent.value}, subtype={result.subtype}")
            return self._build_persona_response(result)
        else:
            print("  [Router] Layer 1: No match")
        
        # ========== Layer 2: NLP (if available) ==========
        if self.layer2:
            print("  [Router] Trying Layer 2 (NLP embedding similarity)...")
            result = self.layer2.classify(query)
            
            if result:
                print(f"  [Router] Layer 2 MATCHED: intent={result.intent.value}, confidence={result.confidence:.3f}")
                
                if result.intent == IntentType.PERSONA:
                    return self._build_persona_response(result)
                elif result.intent == IntentType.OUT_OF_SCOPE:
                    return self._build_out_of_scope_response(result)
                else:
                    return self._build_document_response(result)
            else:
                print("  [Router] Layer 2: No confident match")
        else:
            print("  [Router] Layer 2: Skipped (embeddings not available)")
        
        # ========== Layer 3: LLM (if available) ==========
        if self.layer3:
            print("  [Router] Trying Layer 3 (LLM binary classifier)...")
            result = self.layer3.classify(query)
            
            if result:
                print(f"  [Router] Layer 3 RESULT: intent={result.intent.value}, confidence={result.confidence:.2f}, tokens={result.tokens_used}")
                
                if result.intent == IntentType.PERSONA:
                    print("  [Router] Returning PERSONA response")
                    return self._build_persona_response(result)
                elif result.intent == IntentType.OUT_OF_SCOPE:
                    print("  [Router] Returning OUT_OF_SCOPE response")
                    return self._build_out_of_scope_response(result)
                else:
                    print("  [Router] Intent is DOCUMENT, proceeding to search")
                    return self._build_document_response(result)
            else:
                print("  [Router] Layer 3: LLM classification failed")
        else:
            print("  [Router] Layer 3: Skipped (LLM provider not available)")
        
        # ========== Default: Document search ==========
        print("  [Router] DEFAULT: No classification match, proceeding to document search")
        return self._build_document_response()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get router and classifier statistics"""
        stats = {
            "layers_enabled": {
                "layer1_rule_based": True,
                "layer2_nlp": self.layer2 is not None,
                "layer3_llm": self.layer3 is not None
            }
        }
        
        if self.layer1:
            stats["layer1"] = self.layer1.get_pattern_stats()
        
        if self.layer2:
            stats["layer2"] = self.layer2.get_stats()
        
        if self.layer3:
            stats["layer3"] = self.layer3.get_stats()
        
        return stats
