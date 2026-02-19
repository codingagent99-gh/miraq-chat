#!/usr/bin/env python
"""
Manual test script for GREETING intent.
Tests the complete flow: classification → API building → response generation.
"""

from classifier import classify
from api_builder import build_api_calls
from response_generator import generate_bot_message, generate_suggestions, INTENT_LABELS
from models import Intent


def test_greeting(utterance: str):
    """Test a single greeting utterance through the complete pipeline."""
    print(f"\n{'='*60}")
    print(f"Testing: '{utterance}'")
    print(f"{'='*60}")
    
    # Step 1: Classification
    result = classify(utterance)
    print(f"✓ Intent: {result.intent.value}")
    print(f"✓ Confidence: {result.confidence}")
    
    # Step 2: API Building
    api_calls = build_api_calls(result)
    print(f"✓ API Calls: {len(api_calls)} (expected 0 for greetings)")
    
    # Step 3: Response Generation
    message = generate_bot_message(result.intent, result.entities, [], result.confidence)
    print(f"✓ Bot Message:\n{message}")
    
    suggestions = generate_suggestions(result.intent, result.entities, [])
    print(f"✓ Suggestions: {suggestions}")
    
    # Step 4: Intent Label
    label = INTENT_LABELS.get(result.intent, "unknown")
    print(f"✓ Intent Label: {label}")
    
    # Verify expectations
    assert result.intent == Intent.GREETING, f"Expected GREETING, got {result.intent.value}"
    assert result.confidence == 0.99, f"Expected 0.99 confidence, got {result.confidence}"
    assert len(api_calls) == 0, f"Expected 0 API calls, got {len(api_calls)}"
    assert label == "greeting", f"Expected 'greeting' label, got {label}"
    assert "Hello" in message or "hello" in message, "Message should contain 'Hello'"
    assert len(suggestions) == 4, f"Expected 4 suggestions, got {len(suggestions)}"
    
    print("✓ All checks passed!")


def main():
    """Run manual tests for various greeting phrases."""
    print("\n" + "="*60)
    print("GREETING INTENT - MANUAL VERIFICATION TESTS")
    print("="*60)
    
    # Test various greetings
    test_greetings = [
        "hi",
        "hello",
        "hey",
        "good morning",
        "good evening",
        "how are you?",
        "what's up",
        "howdy",
        "hi there",
    ]
    
    for greeting in test_greetings:
        try:
            test_greeting(greeting)
        except AssertionError as e:
            print(f"✗ FAILED: {e}")
            return 1
    
    print("\n" + "="*60)
    print("Testing non-greetings (should NOT match GREETING intent)")
    print("="*60)
    
    # Test that these DON'T match greeting
    non_greetings = [
        ("hello show me tiles", Intent.PRODUCT_LIST),
        ("hi there do you have marble", Intent.PRODUCT_BY_VISUAL),
        ("good morning show products", Intent.PRODUCT_LIST),
    ]
    
    for utterance, expected_intent in non_greetings:
        print(f"\nTesting: '{utterance}'")
        result = classify(utterance)
        print(f"✓ Intent: {result.intent.value} (expected: {expected_intent.value})")
        if result.intent == Intent.GREETING:
            print(f"✗ FAILED: Should NOT be classified as GREETING")
            return 1
        print("✓ Correctly NOT classified as GREETING")
    
    print("\n" + "="*60)
    print("✓ ALL MANUAL VERIFICATION TESTS PASSED!")
    print("="*60)
    print("\nSummary:")
    print("• Greetings are classified with 0.99 confidence")
    print("• No API calls are generated for greetings")
    print("• Friendly greeting response with 4 helpful suggestions")
    print("• Non-greeting phrases are correctly excluded")
    print("• LLM fallback will NOT be triggered (confidence 0.99 > threshold 0.60)")
    return 0


if __name__ == "__main__":
    exit(main())
