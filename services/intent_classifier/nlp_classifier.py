"""
NLP Classifier (Layer 2)
Embedding similarity matching for intent classification
Used when rule-based patterns don't match
"""

import json
import os
import logging
import time
from typing import Optional, List, Dict, Any
import numpy as np

from .intent_types import IntentType, ClassificationResult

logger = logging.getLogger(__name__)


class NLPClassifier:
    """
    Layer 2 classifier using embedding similarity.
    Compares query embeddings against intent example embeddings.
    """
    
    def __init__(self, embeddings, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the NLP classifier.
        
        Args:
            embeddings: LangChain embeddings object (e.g., AzureOpenAIEmbeddings)
            config: Optional configuration dictionary. If None, loads defaults.
        """
        self.embeddings = embeddings
        self.embeddings_cache: Dict[IntentType, List[np.ndarray]] = {}
        self.cache_initialized = False
        
        # Load config
        self._load_config(config)
        
        if embeddings:
            logger.debug("NLP classifier initialized")
            # Proactively precompute if possible, or leave it for lazy load
            # self.precompute_embeddings() 
        else:
            logger.warning("NLP classifier initialized without embeddings - will be skipped")
            
    def _load_config(self, config: Optional[Dict[str, Any]]):
        """Load configuration from dict or defaults"""
        if config:
            self.examples = {
                IntentType.PERSONA: config.get("layer2_nlp_examples", {}).get("persona", []),
                IntentType.OUT_OF_SCOPE: config.get("layer2_nlp_examples", {}).get("out_of_scope", [])
            }
            thresholds = config.get("thresholds", {})
            self.confidence_threshold = thresholds.get("layer2_nlp_confidence", 0.75)
            self.out_of_scope_threshold = thresholds.get("layer2_out_of_scope_threshold", 0.78)
            self.max_weight = thresholds.get("layer2_max_weight", 0.7)
            self.avg_weight = thresholds.get("layer2_avg_weight", 0.3)
        else:
            # Fallback defaults
            self.examples = {
                IntentType.PERSONA: ["hello", "hi", "thank you", "goodbye"],
                IntentType.OUT_OF_SCOPE: ["tell me a joke", "what is 2+2"]
            }
            self.confidence_threshold = 0.75
            self.out_of_scope_threshold = 0.78
            self.max_weight = 0.7
            self.avg_weight = 0.3
            
    def precompute_embeddings(self) -> bool:
        """
        Pre-compute and cache intent example embeddings for this instance.
        """
        if self.cache_initialized:
            return True
            
        if not self.embeddings:
            return False
            
        try:
            start_time = time.time()
            # logger.info("Pre-computing instance intent embeddings...")
            
            self.embeddings_cache = {}
            total_examples = 0
            
            for intent, examples in self.examples.items():
                embeddings_list = []
                # Batch embed if possible, but embed_query is usually single. 
                # Some LangChain embeddings support embed_documents for batch.
                if hasattr(self.embeddings, 'embed_documents') and examples:
                    try:
                        batch_embeddings = self.embeddings.embed_documents(examples)
                        embeddings_list = [np.array(e) for e in batch_embeddings]
                        total_examples += len(examples)
                    except Exception as e:
                        logger.warning(f"Batch embedding failed: {e}. Falling back to single.")
                        for example in examples:
                             try:
                                emb = self.embeddings.embed_query(example)
                                embeddings_list.append(np.array(emb))
                                total_examples += 1
                             except: pass
                else:
                    for example in examples:
                        try:
                            embedding = self.embeddings.embed_query(example)
                            embeddings_list.append(np.array(embedding))
                            total_examples += 1
                        except Exception as e:
                            logger.warning(f"Failed to embed '{example}': {e}")
                
                self.embeddings_cache[intent] = embeddings_list
            
            elapsed = (time.time() - start_time) * 1000
            self.cache_initialized = True
            logger.info(f"Pre-computed {total_examples} embeddings in {elapsed:.0f}ms")
            return True
            
        except Exception as e:
            logger.error(f"Failed to pre-compute embeddings: {e}")
            return False

    def _compute_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """Compute cosine similarity between two vectors"""
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return float(dot_product / (norm1 * norm2))
    
    def classify(self, query: str) -> Optional[ClassificationResult]:
        """
        Classify query using embedding similarity.
        """
        if not query or not query.strip():
            return None
        
        if not self.embeddings:
            return None
        
        # Lazy initialization of cache
        if not self.cache_initialized:
             self.precompute_embeddings()
        
        try:
            total_start = time.time()
            
            # Compute query embedding
            embed_start = time.time()
            query_embedding = np.array(self.embeddings.embed_query(query.strip()))
            embed_time = (time.time() - embed_start) * 1000
            
            # Find best matching intent
            similarity_start = time.time()
            best_intent = None
            best_score = 0.0
            
            for intent, example_embeddings in self.embeddings_cache.items():
                scores = [
                    self._compute_similarity(query_embedding, ex_emb)
                    for ex_emb in example_embeddings
                ]
                
                if scores:
                    max_score = max(scores)
                    avg_score = sum(scores) / len(scores)
                    combined_score = self.max_weight * max_score + self.avg_weight * avg_score
                    
                    if combined_score > best_score:
                        best_score = combined_score
                        best_intent = intent
            
            similarity_time = (time.time() - similarity_start) * 1000
            total_time = (time.time() - total_start) * 1000
            
            # Log timing info
            # logger.debug(f"Layer 2 Timing: embed={embed_time:.0f}ms, similarity={similarity_time:.0f}ms")
            
            # Return result only if confidence exceeds threshold
            if best_intent and best_score >= self.confidence_threshold:
                return ClassificationResult(
                    intent=best_intent,
                    confidence=best_score,
                    layer_used=2,
                    subtype=None,  
                    reasoning=f"NLP similarity match with confidence {best_score:.3f}",
                    llm_used=False,
                    tokens_used=0,
                    metadata={
                        "classification_method": "nlp_similarity",
                        "similarity_score": best_score,
                        "timing_ms": total_time
                    }
                )
            
            return None
            
        except Exception as e:
            logger.error("Layer 2 classification error: %s", e)
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get classifier statistics"""
        return {
            "cache_initialized": self.cache_initialized,
            "has_embeddings": self.embeddings is not None,
            "confidence_threshold": self.confidence_threshold,
            "cached_intents": len(self.embeddings_cache),
            "intent_examples": {
                intent.value: len(examples) 
                for intent, examples in self.examples.items()
            }
        }



