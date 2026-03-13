import time
import logging
from typing import Dict, Any

# Import the existing LLMClient from your project!
from llm_fallback import LLMClient

# Import the new intent classifier components you copied over
from utils.embeddings import get_embedding_function
from services.intent_classifier.intent_router import IntentRouter
from services.intent_classifier.intent_types import IntentType

logger = logging.getLogger("miraq_chat")

# --- Wrapper for your existing LLMClient ---
class SharedLLMAdapter:
    """
    Wraps your existing LLMClient (which handles Mistral via env vars)
    to match the interface expected by the new LLMClassifier (Layer 3).
    """
    def __init__(self):
        # Instantiate just to get the provider name from env vars
        temp_client = LLMClient()
        self.provider_name = f"Shared App Client ({temp_client.provider})"

    def generate_answer(self, system_message: str, user_message: str, temperature: float, max_tokens: int) -> dict:
        try:
            # Instantiate a fresh client for the request
            client = LLMClient()
            
            # Override default params for this specific classification task
            client.temperature = temperature
            client.max_tokens = max_tokens
            
            # Call your existing chat_completion method!
            result = client.chat_completion(
                system_prompt=system_message, 
                user_message=user_message
            )
            
            return {
                "success": True,
                "answer": result["content"],
                "tokens_used": result.get("total_tokens", 0)
            }
        except Exception as e:
            logger.error(f"Layer 3 classification generation failed: {e}")
            return {"success": False, "error": str(e)}


# --- Initialize Global Router ---
logger.info("Initializing 3-Layer Intent Router (Chit-Chat & Out-of-Scope)...")

# 1. Load local sentence-transformers (Layer 2)
try:
    embeddings = get_embedding_function(backend="local") 
except Exception as e:
    logger.warning(f"Could not load local embeddings for Layer 2: {e}")
    embeddings = None

# 2. Hook up shared LLM client (Layer 3)
llm_provider = SharedLLMAdapter()

# 3. Create the router
router = IntentRouter(
    embeddings=embeddings,
    llm_provider=llm_provider,
    enable_layer2=True if embeddings else False,
    enable_layer3=True
)

def route_query(message: str) -> Dict[str, Any]:
    """
    Passes query through the 3 layers. 
    Returns {"handled": True, ...} if it intercepts the message.
    """
    start_time = time.time()
    
    # Run the 3-layer check
    result = router.route(message)
    
    # Map the output back to what your chat.py expects
    if result["intent"] in [IntentType.PERSONA, IntentType.OUT_OF_SCOPE]:
        elapsed = time.time() - start_time
        layer_used = result.get("metadata", {}).get("layer_used", 1)
        
        logger.info(f"Step 0.8: Intercepted by Layer {layer_used} | intent={result['intent'].value}")
        
        return {
            "handled": True,
            "intent": result["intent"].value.lower(),
            "response": result["answer"],
            "metadata": {
                "layer_used": layer_used,
                "classification_method": result.get("metadata", {}).get("classification_method")
            }
        }
        
    return {"handled": False}