"""
Tests for GREETING intent classification and response generation.

Ensures that greetings are:
1. Classified with high confidence (0.99) using regex patterns
2. Don't trigger LLM fallback
3. Don't generate WooCommerce API calls
4. Return appropriate greeting response with suggestions
"""

import pytest
from classifier import classify
from models import Intent
from api_builder import build_api_calls
from response_generator import generate_bot_message, generate_suggestions, INTENT_LABELS


class TestGreetingClassification:
    """Test greeting intent classification with various greeting phrases."""

    def test_hi(self):
        """Test simple 'hi' greeting."""
        result = classify("hi")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hello(self):
        """Test 'hello' greeting."""
        result = classify("hello")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hey(self):
        """Test 'hey' greeting."""
        result = classify("hey")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_good_morning(self):
        """Test 'good morning' greeting."""
        result = classify("good morning")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_good_evening(self):
        """Test 'good evening' greeting."""
        result = classify("good evening")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_good_afternoon(self):
        """Test 'good afternoon' greeting."""
        result = classify("good afternoon")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_how_are_you(self):
        """Test 'how are you' greeting."""
        result = classify("how are you")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_how_are_you_with_question_mark(self):
        """Test 'how are you?' greeting with punctuation."""
        result = classify("how are you?")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_whats_up(self):
        """Test 'what's up' greeting."""
        result = classify("what's up")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_whats_up_no_apostrophe(self):
        """Test 'whats up' greeting without apostrophe."""
        result = classify("whats up")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hows_it_going(self):
        """Test 'how's it going' greeting."""
        result = classify("how's it going")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_howdy(self):
        """Test 'howdy' greeting."""
        result = classify("howdy")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_yo(self):
        """Test 'yo' greeting."""
        result = classify("yo")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_sup(self):
        """Test 'sup' greeting."""
        result = classify("sup")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hi_there(self):
        """Test 'hi there' greeting."""
        result = classify("hi there")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hey_there(self):
        """Test 'hey there' greeting."""
        result = classify("hey there")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hi_with_exclamation(self):
        """Test 'hi!' greeting with exclamation mark."""
        result = classify("hi!")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99

    def test_hello_with_period(self):
        """Test 'hello.' greeting with period."""
        result = classify("hello.")
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99


class TestGreetingNotMatching:
    """Test that greeting patterns don't match non-greeting phrases."""

    def test_hello_show_me_tiles_not_greeting(self):
        """Test that 'hello show me tiles' is NOT classified as greeting."""
        result = classify("hello show me tiles")
        assert result.intent != Intent.GREETING

    def test_hi_there_do_you_have_marble_not_greeting(self):
        """Test that 'hi there do you have marble' is NOT classified as greeting."""
        result = classify("hi there do you have marble")
        assert result.intent != Intent.GREETING

    def test_good_morning_show_products_not_greeting(self):
        """Test that 'good morning show me products' is NOT classified as greeting."""
        result = classify("good morning show me products")
        assert result.intent != Intent.GREETING

    def test_hey_whats_new_not_greeting(self):
        """Test that 'hey whats new' is NOT classified as greeting."""
        result = classify("hey whats new")
        assert result.intent != Intent.GREETING


class TestGreetingAPIBuilder:
    """Test that greeting intent doesn't generate API calls."""

    def test_greeting_no_api_calls(self):
        """Test that greeting intent returns empty API calls list."""
        result = classify("hi")
        api_calls = build_api_calls(result)
        assert api_calls == []
        assert result.api_calls == []

    def test_multiple_greetings_no_api_calls(self):
        """Test that various greetings all return no API calls."""
        greetings = ["hello", "hey", "good morning", "how are you?"]
        for greeting in greetings:
            result = classify(greeting)
            api_calls = build_api_calls(result)
            assert api_calls == [], f"Expected no API calls for '{greeting}'"


class TestGreetingResponseGeneration:
    """Test greeting response message and suggestions."""

    def test_greeting_bot_message(self):
        """Test that greeting generates appropriate bot message."""
        result = classify("hi")
        message = generate_bot_message(
            intent=result.intent,
            entities=result.entities,
            products=[],
            confidence=result.confidence,
        )
        
        # Check that message contains welcome text
        assert "Hello" in message or "hello" in message
        assert "Welcome" in message or "welcome" in message
        assert "help" in message

    def test_greeting_suggestions(self):
        """Test that greeting generates appropriate suggestions."""
        result = classify("hello")
        suggestions = generate_suggestions(
            intent=result.intent,
            entities=result.entities,
            products=[],
        )
        
        # Check that we have suggestions
        assert len(suggestions) == 4
        
        # Check for expected suggestions
        expected_suggestions = [
            "Show me all products",
            "What categories do you have?",
            "Show me marble look tiles",
            "Quick ship tiles",
        ]
        assert suggestions == expected_suggestions

    def test_greeting_intent_label(self):
        """Test that GREETING has correct intent label."""
        assert Intent.GREETING in INTENT_LABELS
        assert INTENT_LABELS[Intent.GREETING] == "greeting"


class TestGreetingHighConfidence:
    """Test that greetings have high confidence to avoid LLM fallback."""

    def test_greeting_confidence_above_threshold(self):
        """Test that greeting confidence is above LLM fallback threshold."""
        # The LLM fallback threshold is typically 0.70-0.80
        # Greetings should have 0.99 confidence to avoid triggering LLM
        result = classify("hi")
        assert result.confidence >= 0.99
        
        result = classify("hello")
        assert result.confidence >= 0.99
        
        result = classify("good morning")
        assert result.confidence >= 0.99


class TestGreetingCaseSensitivity:
    """Test that greetings work regardless of case."""

    def test_uppercase_hi(self):
        """Test 'HI' in uppercase."""
        result = classify("HI")
        assert result.intent == Intent.GREETING

    def test_mixed_case_hello(self):
        """Test 'HeLLo' in mixed case."""
        result = classify("HeLLo")
        assert result.intent == Intent.GREETING

    def test_uppercase_good_morning(self):
        """Test 'GOOD MORNING' in uppercase."""
        result = classify("GOOD MORNING")
        assert result.intent == Intent.GREETING


class TestGreetingWhitespace:
    """Test that greetings handle whitespace correctly."""

    def test_greeting_with_leading_whitespace(self):
        """Test '  hi' with leading whitespace."""
        result = classify("  hi")
        assert result.intent == Intent.GREETING

    def test_greeting_with_trailing_whitespace(self):
        """Test 'hello  ' with trailing whitespace."""
        result = classify("hello  ")
        assert result.intent == Intent.GREETING

    def test_greeting_with_multiple_spaces(self):
        """Test 'good  morning' with multiple spaces - regex still matches."""
        result = classify("good  morning")
        # The \s+ in regex matches one or more spaces, so this still matches
        # This is acceptable behavior - we want to be flexible with whitespace
        assert result.intent == Intent.GREETING
        assert result.confidence == 0.99
