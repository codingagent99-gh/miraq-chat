import requests
import json
from datetime import datetime

# === Configuration ===
API_URL = "http://localhost:5009/chat"   # Change to your backend Chat endpoint
# API_URL = "https://silfratech.in/chatbot/api/chat"   # Change to your backend Chat endpoint

LOG_FILE = "miraq_chat_test_results.txt"

# === Questions to Test ===
# QUESTIONS = [
#     # Relevant
#     "What tiles do you have?",
#     "Show me floor tiles",
#     "I want marble look tiles",
#     "Search for kitchen tiles",
#     "Find wood finish tiles",
#     "Can I get white glossy tiles?",
#     "What categories are available?",
#     "Show me product categories",
#     "Do you have accessories?",
#     "Show me all categories",
#     "Tell me more about Affogato",
#     "Give details for French Grey",
#     "What are the specifications of Onyx White tile?",
#     "How big is Super White?",
#     "I want to place an order",
#     "Buy 5 Affogato tiles",
#     "Order 10 Classic White tiles",
#     "Can I purchase Raven Black?",
#     "Place an order for Mosaic tile",
#     "I want to reorder my last order",
#     "Add tiles to my cart",
#     "Check my orders",
#     "Show my order status",
#     "Where is my last order?",
#     "What’s the delivery time for my order?",
#     "I want 2x4 size",
#     "Give me matte finish",
#     "Select glazed option",
#     "Ship to 123 Main Street",
#     "Change my delivery address",
#     "Deliver to my office",
#     "I want 10 pieces",
#     "Quantity: 25",
#     "Give me a box",
#     "Yes, confirm order",
#     "No, cancel the order",
#     "Please cancel",
#     "Go back",
#     "Show me more products",
#     "Browse categories",
#     "No, thank you",
#     "Product",
#     "Category",
#     "Order",
#     "Check order status",

#     # Chit chat & Non-Relevant
#     "Hello",
#     "Hi there",
#     "Good morning",
#     "How are you?",
#     "What’s up?",
#     "Who won the cricket match?",
#     "Tell me a joke",
#     "What’s the weather in Paris?",
#     "Who is the president of India?",
#     "Are you human?",
#     "Sing me a song",
#     "What do you do in your free time?",
#     "Who built you?",
#     "No",
#     "No, that’s all",
#     "Nothing else",
#     "Bye",
#     "See you later",
#     "Done",
#     "That’s it",
#     "Nope",
#     "Wait, show me products",
#     "Let’s start over",
#     "Go back to start",
#     "I want something else",
#     "asdfkjh",
#     "???",
#     "/help",
#     "xyz",
#     "Hi! Can I order 5 glossy tiles?",
#     "Good afternoon, I’d like to check my order",
#     "Hello, what categories do you have?",
#     "I want to buy tiles. What options are there?",
#     "Bye, thank you for your help!",
#     "",
#     "🙂",
#     "So I was just wondering, can you maybe tell me if there’s anything interesting you’d like to share?",
#     "¿Tienes baldosas blancas?"
# ]

QUESTIONS = [
'Show me white tone countertop tiles with a glossy finish.',
'Do you have rectified edge countertops in Taupe tones?',
'I’m looking for a minimalistic look countertop, what do you suggest?',
'Which countertop products are from the Titan Marbles Series?',
'Show me 7/16" thick black tone countertops.',
'Do you have countertops with a dimensional look?',
'What countertop tiles are made in Sri Lanka?',
'Compare matte vs glossy finish countertops in gray tones.',
'Show me white tone countertops under $500 with rectified edges that are in the Titan Marbles Series',
'Category: Exterior',
'Show me exterior wall/floor tiles in Tan tones.',
'What exterior tiles come in 7/16" Thick?',
'Do you have black tone exterior tiles with a matte finish?',
'Recommend durable exterior pavers in Gray tones.',
'Show me rectified edge exterior tiles in Taupe tones.',
'Which exterior products give a dimensional look?',
'Show me gray tone exterior tiles with glossy finish that are 7/16" thick.',
'Category: Floor',
'Show me floor tiles in white tones with matte finish.',
'Do you have 7/16" thick floor tiles in black tones?',
'What are the popular gray tone flooring options?',
'Recommend a minimalistic look floor tile.',
'Show me rectified edge floor tiles from Titan Marbles Series.',
'Which floor tiles are made in Sri Lanka?',
'Show me products originating from Sri Lanka.',
'Show me products with Sri Lanka as the country of origin.',
'Compare 1/4" thick vs 7/16" thick floor tiles.',
'Show me brown tone floor tiles with dimensional look that are currently in Popular Collections.',
'Category: Interior',
'Show me interior wall tiles in beige tones.',
'What glossy finish interior tiles are available in gray tones?',
'Do you have minimalistic look interior tiles in white tones?',
'Show me rectified edge interior tiles in Taupe tones.',
'Which interior tiles belong to the Titan Marbles Series?',
'Show me white tone interior tiles with matte finish that are 1/4" thick.',
'Category: Mosaics',
'Show me mosaic look tiles in black tones.',
'Do you have white tone mosaics with glossy finish?',
'Show me 7/16" thick mosaic tiles.',
'What mosaic tiles give a dimensional look?',
'Are there rectified edge mosaics in Taupe tones?',
'Show me mosaics under the Wilde tag.',
'What is the sale and regular price of ALLSPICE Calacatta Oro Silky 3"x3"?',
'Do you have ALLSPICE Brilho Azul Silky 1 7/8"x7 3/8" Chip Size? What is the price?',
'What is the price of ANSEL Charcoal Polished 3"x3"?',
'How much is ANSEL Mica Matte 6"x6"?',
'Compare TITAN MARBLES Onice Ghiaccio Matte vs Polished 3"x3" pricing.',
'Which 12"x24" sample has the highest price?',
'Show me 3"x3" samples under $40.',
'Which products are available in Chip Card size?',
'What sizes are available for ANSEL Charcoal Matte?',
'What sizes are available for WATERFALL Havana Ribbed?',
'What sizes are available for DIVINE Copper Chevron Matte?',
'Which finishes are available for ANSEL Warm White?',
'Which products have 15"x15" size?',
'Which products have 10"x20" size?',
"what finish on ansel",
"what color do you have for ansel mosaic"
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