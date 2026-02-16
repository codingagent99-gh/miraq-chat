#!/usr/bin/env bash
# ───────────────���─────────────────────────────
# Quick run script — use after initial setup
# Usage: ./run.sh [mode]
#   ./run.sh              → Run all test utterances
#   ./run.sh interactive  → Interactive chat mode
#   ./run.sh evaluate     → Run accuracy evaluation
#   ./run.sh test         → Run pytest suite
#   ./run.sh live         → Live mode (makes real API calls)
# ─────────────────────────────────────────────

set -e

# Activate venv
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
fi

MODE=${1:-"default"}

case $MODE in
    "interactive" | "chat" | "i")
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  🗣️  WGC Tiles — Interactive Mode"
        echo "  Type 'quit' or 'exit' to stop"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        python3 -c "
import sys
sys.path.insert(0, '.')
from main import process

while True:
    try:
        query = input('\n💬 You: ').strip()
        if query.lower() in ('quit', 'exit', 'q'):
            print('👋 Goodbye!')
            break
        if query:
            process(query)
    except KeyboardInterrupt:
        print('\n👋 Goodbye!')
        break
    except EOFError:
        break
"
        ;;

    "evaluate" | "eval" | "e")
        echo "📊 Running classifier evaluation..."
        python3 -m training.evaluate
        ;;

    "test" | "t")
        echo "🧪 Running test suite..."
        pytest tests/ -v --tb=short
        ;;

    "live" | "l")
        echo "🌐 Running in LIVE mode (real API calls)..."
        echo "⚠️  Make sure .env has valid API keys!"
        python3 -c "
import sys
sys.path.insert(0, '.')
from main import process
from services.woo_client import WooCommerceClient
from core.classifier import classify
from core.api_builder import build_api_calls

client = WooCommerceClient()

while True:
    try:
        query = input('\n💬 You: ').strip()
        if query.lower() in ('quit', 'exit', 'q'):
            break
        if query:
            result = classify(query)
            calls = build_api_calls(result)
            process(query)

            execute = input('\n🚀 Execute API call? (y/n): ').strip().lower()
            if execute == 'y':
                responses = client.execute_all(calls)
                for r in responses:
                    print(f'\n📡 {r[\"description\"]}:')
                    if r['response']['success']:
                        data = r['response']['data']
                        if isinstance(data, list):
                            print(f'   Returned {len(data)} items')
                            for item in data[:3]:
                                print(f'   • {item.get(\"name\", \"?\")} (ID: {item.get(\"id\", \"?\")})')
                        else:
                            print(f'   {data}')
                    else:
                        print(f'   ❌ Error: {r[\"response\"][\"error\"]}')
    except KeyboardInterrupt:
        print('\n👋 Goodbye!')
        break
"
        ;;

    *)
        echo "🏃 Running all test utterances..."
        python3 main.py
        ;;
esac