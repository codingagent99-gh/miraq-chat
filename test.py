import requests
import json
from datetime import datetime

# === Configuration ===
API_URL = "http://localhost:5009/chat"   # Change to your backend Chat endpoint
LOG_FILE = "miraq_chat_test_results.txt"

# === Questions to Test ===
QUESTIONS = [
    # Relevant
    "What tiles do you have?",
    "Show me floor tiles",
    "I want marble look tiles",
    "Search for kitchen tiles",
    "Find wood finish tiles",
    "Can I get white glossy tiles?",
    "What categories are available?",
    "Show me product categories",
    "Do you have accessories?",
    "Show me all categories",
    "Tell me more about Affogato",
    "Give details for French Grey",
    "What are the specifications of Onyx White tile?",
    "How big is Super White?",
    "I want to place an order",
    "Buy 5 Affogato tiles",
    "Order 10 Classic White tiles",
    "Can I purchase Raven Black?",
    "Place an order for Mosaic tile",
    "I want to reorder my last order",
    "Add tiles to my cart",
    "Check my orders",
    "Show my order status",
    "Where is my last order?",
    "What’s the delivery time for my order?",
    "I want 2x4 size",
    "Give me matte finish",
    "Select glazed option",
    "Ship to 123 Main Street",
    "Change my delivery address",
    "Deliver to my office",
    "I want 10 pieces",
    "Quantity: 25",
    "Give me a box",
    "Yes, confirm order",
    "No, cancel the order",
    "Please cancel",
    "Go back",
    "Show me more products",
    "Browse categories",
    "No, thank you",
    "Product",
    "Category",
    "Order",
    "Check order status",

    # Chit chat & Non-Relevant
    "Hello",
    "Hi there",
    "Good morning",
    "How are you?",
    "What’s up?",
    "Who won the cricket match?",
    "Tell me a joke",
    "What’s the weather in Paris?",
    "Who is the president of India?",
    "Are you human?",
    "Sing me a song",
    "What do you do in your free time?",
    "Who built you?",
    "No",
    "No, that’s all",
    "Nothing else",
    "Bye",
    "See you later",
    "Done",
    "That’s it",
    "Nope",
    "Wait, show me products",
    "Let’s start over",
    "Go back to start",
    "I want something else",
    "asdfkjh",
    "???",
    "/help",
    "xyz",
    "Hi! Can I order 5 glossy tiles?",
    "Good afternoon, I’d like to check my order",
    "Hello, what categories do you have?",
    "I want to buy tiles. What options are there?",
    "Bye, thank you for your help!",
    "",
    "🙂",
    "So I was just wondering, can you maybe tell me if there’s anything interesting you’d like to share?",
    "¿Tienes baldosas blancas?"
]

def send_message_to_chat_api(message, session_id="test-session"):
    """Send a message to the MiraQ chat API."""
    payload = {
        "message": message,
        "session_id": session_id,
    }
    try:
        response = requests.post(API_URL, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def main():
    log_lines = []
    log_lines.append("# MiraQ Chat Test Results")
    log_lines.append(f"Test started: {datetime.now().isoformat()}\n")
    
    for idx, question in enumerate(QUESTIONS, 1):
        result = send_message_to_chat_api(question)
        log_lines.append(f"\n## {idx}. Input: {repr(question)}")
        log_lines.append("```json")
        log_lines.append(json.dumps(result, indent=2, ensure_ascii=False))
        log_lines.append("```")
    
    log_lines.append(f"\nTest ended: {datetime.now().isoformat()}")

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    print(f"\nDone. Results written to {LOG_FILE}")

if __name__ == "__main__":
    main()