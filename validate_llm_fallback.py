#!/usr/bin/env python3
"""
Validation script for LLM fallback integration.
Runs a series of checks to ensure all components are properly configured.
"""

import sys
import os

# Color codes for terminal output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'

def print_status(message, success):
    """Print colored status message."""
    if success:
        print(f"{GREEN}✓{RESET} {message}")
    else:
        print(f"{RED}✗{RESET} {message}")

def print_warning(message):
    """Print warning message."""
    print(f"{YELLOW}⚠{RESET} {message}")

def check_imports():
    """Check that all required modules can be imported."""
    print("\n=== Checking Module Imports ===")
    
    checks = [
        ("app_config", "Application configuration"),
        ("llm_fallback", "LLM fallback module"),
        ("routes.chat", "Chat routes"),
        ("classifier", "Intent classifier"),
        ("models", "Data models"),
    ]
    
    all_ok = True
    for module, description in checks:
        try:
            __import__(module)
            print_status(f"{description} ({module})", True)
        except Exception as e:
            print_status(f"{description} ({module}): {str(e)}", False)
            all_ok = False
    
    return all_ok

def check_config():
    """Check that LLM configuration is present."""
    print("\n=== Checking LLM Configuration ===")
    
    from app_config import (
        LLM_PROVIDER,
        LLM_MODEL,
        LLM_FALLBACK_ENABLED,
        LLM_RETRY_ON_EMPTY_RESULTS,
    )
    
    print(f"  Provider: {LLM_PROVIDER}")
    print(f"  Model: {LLM_MODEL}")
    print(f"  Fallback Enabled: {LLM_FALLBACK_ENABLED}")
    print(f"  Retry on Empty: {LLM_RETRY_ON_EMPTY_RESULTS}")
    
    # Check for API credentials
    if LLM_PROVIDER == "copilot":
        from app_config import COPILOT_API_TOKEN
        has_creds = bool(COPILOT_API_TOKEN)
        cred_name = "COPILOT_API_TOKEN"
    else:
        from app_config import LLM_API_KEY
        has_creds = bool(LLM_API_KEY)
        cred_name = "LLM_API_KEY"
    
    if has_creds:
        print_status(f"{cred_name} is set", True)
    else:
        print_warning(f"{cred_name} is not set (LLM calls will fail)")
    
    return True

def check_sanitization():
    """Check PII sanitization functions."""
    print("\n=== Checking PII Sanitization ===")
    
    from llm_fallback import _sanitize_for_llm
    
    tests = [
        ("Email: test@example.com", "[EMAIL]", "email"),
        ("Phone: 555-123-4567", "[PHONE]", "phone"),
        ("I want marble tiles", "marble tiles", "normal text"),
    ]
    
    all_ok = True
    for input_text, expected_in_output, test_name in tests:
        output = _sanitize_for_llm(input_text)
        if expected_in_output in output:
            print_status(f"Sanitize {test_name}", True)
        else:
            print_status(f"Sanitize {test_name}: expected '{expected_in_output}' in '{output}'", False)
            all_ok = False
    
    return all_ok

def check_llm_client():
    """Check LLM client initialization."""
    print("\n=== Checking LLM Client ===")
    
    try:
        from llm_fallback import LLMClient
        from app_config import LLM_PROVIDER
        
        client = LLMClient()
        print_status(f"LLMClient initialized with provider: {client.provider}", True)
        
        if client.provider.lower() != LLM_PROVIDER.lower():
            print_warning(f"Provider mismatch: config={LLM_PROVIDER}, client={client.provider}")
        
        return True
    except Exception as e:
        print_status(f"LLMClient initialization failed: {str(e)}", False)
        return False

def check_integration():
    """Check integration points in chat.py."""
    print("\n=== Checking Integration Points ===")
    
    try:
        # Check that imports work
        from routes.chat import chat_bp
        print_status("Chat blueprint imported", True)
        
        # Check that LLM fallback is imported
        import routes.chat as chat_module
        if hasattr(chat_module, 'llm_fallback'):
            print_status("llm_fallback imported in chat.py", True)
        else:
            print_warning("llm_fallback not found in chat.py imports")
        
        return True
    except Exception as e:
        print_status(f"Integration check failed: {str(e)}", False)
        return False

def check_tests():
    """Check if tests can run."""
    print("\n=== Checking Tests ===")
    
    try:
        import pytest
        print_status("pytest is installed", True)
        
        # Check if test file exists
        if os.path.exists("test_llm_fallback.py"):
            print_status("test_llm_fallback.py exists", True)
        else:
            print_status("test_llm_fallback.py not found", False)
            return False
        
        return True
    except ImportError:
        print_status("pytest not installed", False)
        return False

def main():
    """Run all validation checks."""
    print("=" * 60)
    print("LLM Fallback Module - Validation Script")
    print("=" * 60)
    
    results = []
    
    results.append(("Module Imports", check_imports()))
    results.append(("Configuration", check_config()))
    results.append(("PII Sanitization", check_sanitization()))
    results.append(("LLM Client", check_llm_client()))
    results.append(("Integration", check_integration()))
    results.append(("Tests", check_tests()))
    
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        print_status(name, success)
    
    print(f"\nPassed: {passed}/{total}")
    
    if passed == total:
        print(f"\n{GREEN}✓ All checks passed! LLM fallback is ready to use.{RESET}")
        return 0
    else:
        print(f"\n{YELLOW}⚠ Some checks failed. Review the output above.{RESET}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
