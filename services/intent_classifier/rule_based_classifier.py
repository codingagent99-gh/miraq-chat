"""
Rule-Based Classifier (Layer 1)
Fast, deterministic pattern matching for persona queries
"""

import json
import os
import re
import logging
from typing import Optional, List, Tuple, Dict, Any

from .intent_types import IntentType, ClassificationResult

logger = logging.getLogger(__name__)


# Load patterns from consolidated config file
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config', 'intent_classifier_config.json')

def _load_config() -> Dict[str, Any]:
    """Load regex patterns from JSON config file"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return {
                "patterns": config.get("layer1_regex_patterns", {}),
                "confidence": config.get("thresholds", {}).get("layer1_rule_confidence", 1.0)
            }
    except Exception as e:
        logger.warning(f"Could not load intent_classifier_config.json: {e}")
        # Return minimal defaults if file not found
        return {
            "patterns": {
                "greeting": ["^\\s*(hi|hello|hey)\\s*[!.,?]*\\s*$"],
                "gratitude": ["^\\s*(thanks?|thank\\s+you)\\s*[!.,]*\\s*$"],
                "farewell": ["^\\s*(bye|goodbye)\\s*[!.,]*\\s*$"]
            },
            "confidence": 1.0
        }

# Load config
_raw_config = _load_config()
_raw_patterns = _raw_config.get("patterns", {})
PERSONA_PATTERNS = {k: v for k, v in _raw_patterns.items() if not k.startswith("_")}
LAYER1_CONFIDENCE = _raw_config.get("confidence", 1.0)


def reload_config():
    """Reload config from JSON (for runtime updates)"""
    global _raw_config, _raw_patterns, PERSONA_PATTERNS, LAYER1_CONFIDENCE
    _raw_config = _load_config()
    _raw_patterns = _raw_config.get("patterns", {})
    PERSONA_PATTERNS = {k: v for k, v in _raw_patterns.items() if not k.startswith("_")}
    LAYER1_CONFIDENCE = _raw_config.get("confidence", 1.0)
    logger.info(f"Layer 1 config reloaded: {len(PERSONA_PATTERNS)} pattern groups, confidence={LAYER1_CONFIDENCE}")




class RuleBasedClassifier:
    """
    Layer 1 classifier using regex pattern matching.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the classifier with compiled regex patterns.
        
        Args:
            config: Optional configuration dictionary. If None, loads from default file.
        """
        if config:
            self.patterns_config = config.get("layer1_regex_patterns", {})
            self.confidence = config.get("thresholds", {}).get("layer1_rule_confidence", 1.0)
        else:
            # Fallback to module-level defaults (loaded from file)
            self.patterns_config = PERSONA_PATTERNS
            self.confidence = LAYER1_CONFIDENCE
            
        # Filter out description keys
        self.patterns_config = {k: v for k, v in self.patterns_config.items() if not k.startswith("_")}

        self._compiled_patterns = {}
        for subtype, patterns in self.patterns_config.items():
            try:
                self._compiled_patterns[subtype] = [
                    re.compile(pattern, re.IGNORECASE)
                    for pattern in patterns
                ]
            except re.error as e:
                logger.error(f"Invalid regex pattern in subtype '{subtype}': {e}")
                
        logger.info("Rule-based classifier initialized with %d pattern groups (confidence=%s)", 
                   len(self._compiled_patterns), self.confidence)
    
    def classify(self, query: str) -> Optional[ClassificationResult]:
        """
        Classify query using rule-based pattern matching.
        
        Args:
            query: The user's query string
            
        Returns:
            ClassificationResult if persona intent detected, None otherwise
        """
        if not query or not query.strip():
            return None
        
        normalized_query = query.strip().lower()
        
        # Check each pattern group
        for subtype, patterns in self._compiled_patterns.items():
            for pattern in patterns:
                if pattern.search(normalized_query):
                    logger.debug(
                        "Layer 1 match: subtype='%s', pattern='%s', query='%s'",
                        subtype, pattern.pattern, query[:50]
                    )
                    
                    return ClassificationResult(
                        intent=IntentType.PERSONA,
                        confidence=self.confidence,
                        layer_used=1,
                        subtype=subtype,
                        reasoning=f"Matched rule-based pattern: {subtype}",
                        llm_used=False,
                        tokens_used=0,
                        metadata={
                            "classification_method": "rule_based",
                            "matched_pattern": subtype
                        }
                    )
        
        # No match found
        logger.debug("Layer 1: No pattern match for query='%s'", query[:50])
        return None
    
    def get_pattern_stats(self) -> dict:
        """Get statistics about loaded patterns"""
        stats = {}
        for subtype, patterns in self._compiled_patterns.items():
            stats[subtype] = len(patterns)
        return {
            "total_subtypes": len(stats),
            "patterns_per_subtype": stats,
            "total_patterns": sum(stats.values())
        }
