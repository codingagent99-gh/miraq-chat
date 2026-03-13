"""
Intent Types and Data Classes
Defines enums and result structures for intent classification
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


class IntentType(Enum):
    """Primary intent categories"""
    PERSONA = "PERSONA"           # Greetings, chit-chat, bot identity
    DOCUMENT = "DOCUMENT"         # Requires document/knowledge base lookup
    OUT_OF_SCOPE = "OUT_OF_SCOPE" # Questions outside application scope


class PersonaSubtype(Enum):
    """Subtypes for persona queries"""
    GREETING = "greeting"
    WELLBEING = "wellbeing"
    BOT_IDENTITY = "bot_identity"
    GRATITUDE = "gratitude"
    FAREWELL = "farewell"
    CAPABILITIES = "capabilities"
    UNKNOWN = "unknown"


@dataclass
class ClassificationResult:
    """Result from intent classification"""
    intent: IntentType
    confidence: float
    layer_used: int  # 1 = rule-based, 2 = NLP, 3 = LLM
    subtype: Optional[str] = None
    reasoning: Optional[str] = None
    llm_used: bool = False
    tokens_used: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "layer_used": self.layer_used,
            "subtype": self.subtype,
            "reasoning": self.reasoning,
            "llm_used": self.llm_used,
            "tokens_used": self.tokens_used,
            "metadata": self.metadata
        }
