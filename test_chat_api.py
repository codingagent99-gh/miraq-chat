"""
Test the /chat endpoint.
Run: python test_chat_api.py
"""

import requests
import json

BASE = "http://localhost:5009"

def test(message, session_id="test_session_001"):
    print(f"\n{'━'*60}")
    print(f"💬 {message}")
    print(f"{'━'*60}")

    resp = requests.post(f"{BASE}/chat", json={
        "message": message,
        "session_id": session_id,
        "user_context": {
            "customer_id": 130,
            "email": "aecordovez@hotmail.com",
        }
    })

    data = resp.json()
    print(f"✅ Status: {resp.status_code}")
    print(f"🎯 Intent: {data.get('intent')}")
    print(f"🤖 Bot: {data.get('bot_message', '')[:200]}")
    print(f"📦 Products: {data.get('metadata', {}).get('products_count', 0)}")
    print(f"📊 Confidence: {data.get('metadata', {}).get('confidence', 0)}")
    print(f"💡 Suggestions: {data.get('suggestions', [])}")

    if data.get("products"):
        for p in data["products"][:3]:
            print(f"   • {p['name']} (${p['price']}) — {p['permalink']}")

    return data


if __name__ == "__main__":
    # Health check
    print("🏥 Health check...")
    r = requests.get(f"{BASE}/health")
    print(f"   {r.json()}")

    # Test queries
    tests = [
        "show affogato chip card",
        "Show me wall tiles",
        "What categories do you have?",
        "Show me marble look tiles",
        "Matte finish tiles",
        "Quick ship tiles",
        "Tell me about Akard",
        "What colors does Affogato come in?",
        "Show me mosaic tiles",
        "Is there a coupon code?",
        "2023 collection",
        "Made in Italy tiles",
    ]

    session_id = "test_session_001"
    for msg in tests:
        test(msg, session_id)

    # Check session history
    print(f"\n{'━'*60}")
    print("📜 Session History:")
    r = requests.get(f"{BASE}/session/{session_id}")
    history = r.json().get("session", {}).get("history", [])
    print(f"   {len(history)} messages in session")