"""
Intent Classification Service
Wrapper service that integrates 3-layer intent classification into search API.
Updated to work with ANY LLM provider (OpenAI, Mistral Local, Mistral Cloud)
"""

import json
import os
import time
import logging
from typing import Dict, Any, Optional

from services.intent_classifier import IntentRouter, IntentType, get_persona_response, OUT_OF_SCOPE_RESPONSE
from services.intent_classifier.rule_based_classifier import RuleBasedClassifier
from services.intent_classifier.nlp_classifier import NLPClassifier
from services.intent_classifier.llm_classifier import LLMClassifier

logger = logging.getLogger(__name__)


class IntentClassificationService:
    """
    Service that handles 3-layer intent classification before document search.
    Fully multi-tenant: loads config and classifiers per organization.
    NOW SUPPORTS ALL LLM PROVIDERS (OpenAI, Mistral Local, Mistral Cloud)
    """
    
    _embeddings_cache = None
    _classifier_cache: Dict[str, Dict[str, Any]] = {}  # org_id -> {'classifiers': ..., 'config': ...}
    
    @classmethod
    def clear_cache(cls, org_id):
        """Clear cached classifiers for an organization (call after config update)"""
        if isinstance(org_id, str):
            org_id = int(org_id)
        
        if org_id in cls._classifier_cache:
            del cls._classifier_cache[org_id]
            logger.info(f"✓ Cleared intent classifier cache for org {org_id}")
            print(f"\n{'='*60}")
            print(f"CACHE CLEARED for org {org_id}")
            print(f"Next query will reload fresh config from database")
            print(f"{'='*60}\n")
        else:
            logger.info(f"No cache to clear for org {org_id}")

    @classmethod
    def _get_embeddings(cls):
        """Get embeddings for NLP classifier (cached globally)"""
        if cls._embeddings_cache is None:
            try:
                from utils.embeddings import get_embedding_function
                cls._embeddings_cache = get_embedding_function(backend="nomic")
                logger.info("Embeddings loaded for intent classifier")
            except Exception as e:
                logger.warning(f"Could not load embeddings: {e}")
                return None
        return cls._embeddings_cache
    
    @classmethod
    def _get_llm_provider(cls, org=None, db=None):
        """
        Get LLM provider for intent classification.
        Uses LLMService to automatically select right provider (OpenAI/Mistral/MistralCloud)
        
        UPDATED: Now provider-agnostic - works with any LLM provider
        """
        try:
            from services.llm_service import LLMService
            
            # Create LLM service - it will automatically select right provider
            if org:
                llm_service = LLMService(
                    user=None,
                    request_id=f"intent_{org.id}",
                    db_session=db,
                    organization=org
                )
            else:
                llm_service = LLMService(user=None, request_id="intent_system", db_session=db)
            
            provider = llm_service.provider
            provider_name = getattr(provider, 'provider_name', 'unknown')
            
            logger.info(f"LLM provider loaded for intent classifier: {provider_name}")
            return provider
            
        except Exception as e:
            logger.warning(f"Could not load LLM provider: {e}")
            return None

    @classmethod
    def _get_org_components(cls, org_id, org=None, db=None):
        """
        Get or initialize classifiers and config for an organization.
        Returns: (config_dict, classifiers_dict)
        
        UPDATED: Now passes org and db to _get_llm_provider
        """
        if isinstance(org_id, str):
            org_id = int(org_id)
        
        print(f"\n{'='*60}")
        print(f"LOADING INTENT CLASSIFIER CONFIG FOR ORG ID: {org_id}")
        print(f"{'='*60}")
        
        # Check cache first
        if org_id in cls._classifier_cache:
            print(f"Config found in CACHE for org {org_id}")
            config_dict = cls._classifier_cache[org_id]['config']
            classifiers = cls._classifier_cache[org_id]['classifiers']
            print(f"  Layers cached: {list(classifiers.keys())}")
            print(f"{'='*60}\n")
            return config_dict, classifiers

        print(f"Config NOT in cache - loading from DATABASE...")
        
        # Load config from DB
        from services.intent_classifier_config_service import IntentClassifierConfigService
        config_dict = IntentClassifierConfigService.get_config(org_id)
        
        print(f"\nLOADED CONFIGURATION:")
        print(f"  Source: Database (org_id={org_id})")
        print(f"\n  Master Switches:")
        print(f"    INTENT_QUERY_CLASSIFIER_ENABLED: {config_dict.get('INTENT_QUERY_CLASSIFIER_ENABLED', 'NOT SET')}")
        print(f"    LAYER1_ENABLED (Regex): {config_dict.get('LAYER1_ENABLED', 'NOT SET')}")
        print(f"    LAYER2_ENABLED (NLP): {config_dict.get('LAYER2_ENABLED', 'NOT SET')}")
        print(f"    LAYER3_ENABLED (LLM): {config_dict.get('LAYER3_ENABLED', 'NOT SET')}")

        # Initialize classifiers with this config
        classifiers = {}
        print(f"\n🔧 INITIALIZING CLASSIFIERS:")
        
        # Layer 1
        if config_dict.get('LAYER1_ENABLED', True):
            classifiers['layer1'] = RuleBasedClassifier(config=config_dict)
            print(f"  ✓ Layer 1 (Rule-based) initialized")
        else:
            print(f"  ✗ Layer 1 DISABLED")
            
        # Layer 2
        if config_dict.get('LAYER2_ENABLED', True):
            embeddings = cls._get_embeddings()
            if embeddings:
                classifiers['layer2'] = NLPClassifier(embeddings=embeddings, config=config_dict)
                print(f"  ✓ Layer 2 (NLP) initialized with embeddings")
            else:
                print(f"  ⚠ Layer 2 skipped - no embeddings available")
        else:
            print(f"  ✗ Layer 2 DISABLED")
        
        # Layer 3 - UPDATED to use any provider
        if config_dict.get('LAYER3_ENABLED', True):
            llm_provider = cls._get_llm_provider(org=org, db=db)
            if llm_provider:
                provider_name = getattr(llm_provider, 'provider_name', 'unknown')
                classifiers['layer3'] = LLMClassifier(llm_provider=llm_provider, config=config_dict)
                print(f"  ✓ Layer 3 (LLM) initialized with {provider_name} provider")
            else:
                print(f"  ⚠ Layer 3 skipped - no LLM provider available")
        else:
            print(f"  ✗ Layer 3 DISABLED")

        # Cache valid components
        cls._classifier_cache[org_id] = {
            'config': config_dict,
            'classifiers': classifiers
        }
        
        print(f"\nConfig cached for org {org_id}")
        print(f"  Active layers: {list(classifiers.keys())}")
        print(f"{'='*60}\n")
        
        return config_dict, classifiers
    
    @classmethod
    def _get_predefined_response(cls, intent_type: str, subtype: str, config: Dict) -> str:
        """
        Get predefined response from config without calling LLM.
        Used for Layer 1 and Layer 2 - only Layer 3 should use LLM.
        """
        import random
        
        if intent_type == "PERSONA":
            responses = config.get("persona_responses", {}).get(subtype, [])
            if responses:
                return random.choice(responses)
            fallback = config.get("persona_responses", {}).get("unknown", [])
            if fallback:
                return random.choice(fallback)
            return "Hello! How can I help you today?"
        
        elif intent_type == "OUT_OF_SCOPE":
            return config.get("out_of_scope_response", "I can only answer questions about the provided documents.")
        
        return "I'm here to help! What can I do for you?"
    
    @classmethod
    def _generate_llm_response(cls, intent_type: str, question: str, org=None, db=None, config: Dict = None) -> Dict[str, Any]:
        """
        Generate LLM response for PERSONA or OUT_OF_SCOPE intents.
        UPDATED: Now works with any LLM provider (OpenAI, Mistral Local, Mistral Cloud)
        """
        answer = None
        llm_generated = False
        tokens_used = 0
        provider_used = "predefined"
        
        # Build prompt based on intent
        if intent_type == "PERSONA":
            system_prompt = (
                "You are a helpful assistant. Respond naturally and briefly (1-2 sentences) "
                "to this greeting. Be friendly. Do not mention any app name or your identity."
            )
        else:  # OUT_OF_SCOPE
            system_prompt = (
                "You are a helpful assistant. Answer the user's question directly and helpfully. "
                "Provide a brief, accurate answer (2-3 sentences max). Be informative and friendly. "
                "Do NOT mention your knowledge cutoff date, training data, or any year limitations."
            )
        
        user_prompt = f'User: "{question}"'
        
        # Use LLMService to generate response (works with any provider)
        if org or db:
            try:
                from services.llm_service import LLMService
                llm_service = LLMService(
                    user=None, 
                    request_id=f"intent_{org.id if org else 'system'}", 
                    db_session=db,
                    organization=org
                )
                
                # Use generate_answer method (works with ALL providers)
                result = llm_service.provider.generate_answer(
                    system_message=system_prompt,
                    user_message=user_prompt,
                    temperature=0.7,
                    max_tokens=100
                )
                
                if result and result.get('success'):
                    answer = result.get('answer', '').strip()
                    tokens_used = result.get('tokens_used', 0)
                    llm_generated = True
                    provider_used = llm_service.provider.provider_name
                    
            except Exception as e:
                logger.warning(f"LLM generation failed: {e}")
        
        # Fallback to predefined response from Config
        if not answer:
            if intent_type == "PERSONA":
                answer = "Hello! How can I help you today?" 
            else:
                answer = config.get("out_of_scope_response", "I can only answer questions about the provided documents.") if config else "I can only answer questions about the provided documents."
            
            logger.info("Using predefined fallback response")
            provider_used = "fallback_config"
        
        return {
            "answer": answer,
            "llm_generated": llm_generated,
            "tokens_used": tokens_used,
            "provider": provider_used
        }
    
    @classmethod
    def classify_and_respond(
        cls,
        org,
        db,
        question: str,
        user_role: str,
        start_time: float
    ) -> Dict[str, Any]:
        """
        Run 3-layer intent classification using PER-ORG configuration.
        UPDATED: Now passes org and db to _get_org_components
        """
        if not org:
            logger.warning("No organization provided for intent classification - skipping")
            return {"should_proceed_to_search": True}

        # Get Org Config & Classifiers - UPDATED call
        try:
            config, classifiers = cls._get_org_components(org.id, org=org, db=db)
        except Exception as e:
            logger.error(f"Failed to load intent config for org {org.id}: {e}")
            return {"should_proceed_to_search": True}
            
        # Check if enabled globally for this org
        if not config.get('INTENT_QUERY_CLASSIFIER_ENABLED', False):
            return {"should_proceed_to_search": True}
        
        print(f"\n{'='*60}")
        print(f"INTENT CLASSIFICATION (Org: {org.name})")
        print(f"{'='*60}")
        print(f"Question: {question}")
        
        # Extract thresholds for logic
        thresh = config.get("thresholds", {})
        nlp_oos_threshold = thresh.get("layer2_out_of_scope_threshold", 0.78)
        llm_conf_threshold = thresh.get("layer3_llm_confidence", 0.75)

        # ========== Layer 1: Rule-based Persona Detection ==========
        if config.get('LAYER1_ENABLED', True) and 'layer1' in classifiers:
            print(f"\nLayer 1: Rule-based Persona Check")
            try:
                layer1 = classifiers['layer1']
                persona_result = layer1.classify(question)
            
                if persona_result and persona_result.intent == IntentType.PERSONA:
                    response_time = int((time.time() - start_time) * 1000)
                    print(f"  ✓ MATCHED: {persona_result.subtype}")
                    
                    answer = cls._get_predefined_response("PERSONA", persona_result.subtype, config)
                    print(f"  ✓ Using predefined response (no LLM call)")

                    print(f"{'='*60}\n")
                    
                    return {
                        "should_proceed_to_search": False,
                        "status_code": 200,
                        "response": {
                            "status": "success",
                            "answer": answer,
                            "intent": "PERSONA",
                            "metadata": {
                                "classification_method": "rule_based",
                                "matched_pattern": persona_result.subtype,
                                "layer_used": 1,
                                "llm_generated": False,
                                "tokens_used": 0,
                                "provider": "predefined_config",
                                "question": question,
                                "role": user_role
                            },
                            "response_time_ms": response_time
                        }
                    }
                else:
                    print(f"  → No match - proceeding to Layer 2")
            except Exception as e:
                print(f"  ✗ Error: {e} - proceeding to Layer 2")
        else:
            print(f"\nLayer 1: DISABLED/MISSING - skipping")
        
        # ========== Layer 2: NLP Similarity Check ==========
        if config.get('LAYER2_ENABLED', True) and 'layer2' in classifiers:
            print(f"\nLayer 2: NLP Similarity Check")
            try:
                nlp_classifier = classifiers['layer2']
                nlp_result = nlp_classifier.classify(question)
                
                if nlp_result:
                    print(f"  → Result: intent={nlp_result.intent.value}, confidence={nlp_result.confidence:.3f}")
                    
                    # Handle PERSONA intents with high confidence
                    if nlp_result.intent == IntentType.PERSONA:
                        response_time = int((time.time() - start_time) * 1000)
                        print(f"  ✓ PERSONA detected with confidence {nlp_result.confidence:.2f}")
                        
                        subtype = "unknown"
                        if any(word in question.lower() for word in ["who", "what are you"]):
                            subtype = "bot_identity"
                        elif any(word in question.lower() for word in ["hello", "hi", "hey", "good morning"]):
                            subtype = "greeting"
                        
                        answer = cls._get_predefined_response("PERSONA", subtype, config)
                        print(f"  ✓ Using predefined response for '{subtype}' (no LLM call)")
                        
                        print(f"{'='*60}\n")
                        
                        return {
                            "should_proceed_to_search": False,
                            "status_code": 200,
                            "response": {
                                "status": "success",
                                "answer": answer,
                                "intent": "PERSONA",
                                "metadata": {
                                    "classification_method": "nlp_similarity",
                                    "nlp_confidence": nlp_result.confidence,
                                    "layer_used": 2,
                                    "llm_generated": False,
                                    "tokens_used": 0,
                                    "provider": "predefined_config",
                                    "question": question,
                                    "role": user_role
                                },
                                "response_time_ms": response_time
                            }
                        }
                    
                    # Only intercept for high-confidence OUT_OF_SCOPE
                    elif nlp_result.intent == IntentType.OUT_OF_SCOPE and nlp_result.confidence >= nlp_oos_threshold:
                        response_time = int((time.time() - start_time) * 1000)
                        print(f"  ✓ HIGH CONFIDENCE OUT_OF_SCOPE ({nlp_result.confidence:.2f} >= {nlp_oos_threshold})")
                        
                        answer = cls._get_predefined_response("OUT_OF_SCOPE", "", config)
                        print(f"  ✓ Using predefined out-of-scope response (no LLM call)")
                        
                        print(f"{'='*60}\n")
                        
                        return {
                            "should_proceed_to_search": False,
                            "status_code": 200,
                            "response": {
                                "status": "success",
                                "answer": answer,
                                "intent": "OUT_OF_SCOPE",
                                "metadata": {
                                    "classification_method": "nlp_similarity",
                                    "nlp_confidence": nlp_result.confidence,
                                    "layer_used": 2,
                                    "llm_generated": False,
                                    "tokens_used": 0,
                                    "provider": "predefined_config",
                                    "question": question,
                                    "role": user_role
                                },
                                "response_time_ms": response_time
                            }
                        }
                    else:
                        print(f"  → Confidence {nlp_result.confidence:.3f} < threshold - proceeding to Layer 3")
                else:
                    print(f"  → No confident match - proceeding to Layer 3")
            except Exception as e:
                print(f"  ✗ Error: {e} - proceeding to Layer 3")
        else:
            print(f"\nLayer 2: DISABLED/MISSING - skipping")
        
        # ========== Layer 3: LLM Classification ==========
        if config.get('LAYER3_ENABLED', True) and 'layer3' in classifiers:
            print(f"\nLayer 3: LLM Classification")
            try:
                llm_classifier = classifiers['layer3']
                llm_result = llm_classifier.classify(question)
            
                if llm_result and llm_result.confidence >= llm_conf_threshold:
                    print(f"  → Result: intent={llm_result.intent.value}, confidence={llm_result.confidence:.2f}")
                    
                    if llm_result.intent in [IntentType.PERSONA, IntentType.OUT_OF_SCOPE]:
                        response_time = int((time.time() - start_time) * 1000)
                        intent_type = llm_result.intent.value
                        print(f"  ✓ {intent_type} detected - generating response")
                        
                        # Generate response using any provider
                        gen_result = cls._generate_llm_response(intent_type, question, org, db, config)
                        
                        print(f"{'='*60}\n")
                        
                        return {
                            "should_proceed_to_search": False,
                            "status_code": 200,
                            "response": {
                                "status": "success",
                                "answer": gen_result["answer"],
                                "intent": intent_type,
                                "metadata": {
                                    "classification_method": "llm_classifier",
                                    "llm_confidence": llm_result.confidence,
                                    "layer_used": 3,
                                    "llm_generated": gen_result["llm_generated"],
                                    "tokens_used": gen_result["tokens_used"],
                                    "provider": gen_result["provider"],
                                    "question": question,
                                    "role": user_role
                                },
                                "response_time_ms": response_time
                            }
                        }
                    else:
                        print(f"  → Intent is DOCUMENT - proceeding to document search")
                else:
                    conf = llm_result.confidence if llm_result else 0
                    print(f"  → Low confidence ({conf:.2f}) or no result - proceeding to document search")
            except Exception as e:
                print(f"  ✗ Error: {e} - proceeding to document search")
        else:
            print(f"\nLayer 3: DISABLED/MISSING - skipping")
        
        # ========== Default: Proceed to Document Search ==========
        print(f"\n→ Proceeding to document search")
        print(f"{'='*60}\n")
        
        return {"should_proceed_to_search": True}