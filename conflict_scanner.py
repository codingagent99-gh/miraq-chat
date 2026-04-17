import os
import json
from collections import defaultdict
from chat_logger import get_logger
from classifier import classify
from classifier.utils import normalize_for_tag_compare
from api_builder import build_api_calls

logger = get_logger("miraq_admin")
TEST_FILE_PATH = os.path.join(os.path.dirname(__file__), "test_queries.txt")

def _serialize_entities(entities) -> dict:
    """Safely converts the ExtractedEntities object into a clean dictionary for the frontend."""
    clean_dict = {}
    for key, value in entities.__dict__.items():
        # Only include fields that actually have data to keep the UI clean!
        if value:
            # Convert sets to lists so they can be JSON serialized
            if isinstance(value, set):
                clean_dict[key] = list(value)
            # Convert any inner custom objects (like OR pairs) safely
            elif isinstance(value, list) and len(value) > 0 and not isinstance(value[0], (str, int, float, bool)):
                clean_dict[key] = [dict(v) if hasattr(v, '__dict__') else v for v in value]
            else:
                clean_dict[key] = value
    return clean_dict

def simulate_single_term(term: str) -> dict:
    result = classify(term)
    entities = result.entities
    intent = result.intent
    
    groups = defaultdict(set)
    all_locations = set()
    auto_resolved_locations = set()

    def add_loc(raw_text, loc_string, base_type):
        if not raw_text: return
        tokens = normalize_for_tag_compare(raw_text.replace('-', ' '))
        if tokens:
            all_locations.add(loc_string)
            for token in tokens:
                if len(token) > 2:
                    groups[token].add((loc_string, base_type))

    # --- 1. Map Active Extractions ---
    if entities.product_name:
        add_loc(entities.product_name, f"Product [{entities.product_name}]", "Product")
        
    if getattr(entities, 'target_category_slugs', set()):
        slugs = ", ".join(entities.target_category_slugs)
        cat_name = entities.category_name or slugs 
        add_loc(cat_name, f"Category [{slugs}]", "Category")
        
    if entities.attributes:
        for attr_label, attr_slug in entities.attributes.items():
            add_loc(attr_slug, f"Attr ({attr_label.title()}) [{attr_slug}]", "Attribute")
            
    if entities.tag_slugs:
        for t_slug in entities.tag_slugs:
            add_loc(t_slug, f"Tag [{t_slug}]", "Tag")
            
    # --- 2. Map Resolved Extractions (Bypassing the conflict checker) ---
    if entities.attr_tag_or_pairs:
        for pair in entities.attr_tag_or_pairs:
            t_slug = pair.get("tag_slug") or pair.get("attr_term") or "unknown"
            # Add it to the NEW bucket instead of add_loc()
            auto_resolved_locations.add(f"OR-Pair [{t_slug}]")

    # --- 3. Evaluate True Conflicts ---
    is_conflict = False
    for token, items in groups.items():
        base_types = {item[1] for item in items}
        non_product_types = base_types - {"Product"}
        if len(non_product_types) >= 2:
            is_conflict = True
            break

    real_endpoint = "None"
    real_body = {}
    
    try:
        # Pass our simulated AI result into your actual API builder
        api_calls = build_api_calls(result, page=1)
        if api_calls:
            # Grab the details of the first API call it generated
            call = api_calls[0]
            real_endpoint = f"{call.method} {call.endpoint}"
            # GET requests use params, POST requests use body
            real_body = call.params if call.method == "GET" else call.body
    except Exception as e:
        logger.error(f"Failed to build debug API call: {e}")
        real_body = {"error": str(e)}

    return {
        "locations": sorted(list(all_locations)),
        "auto_resolved": sorted(list(auto_resolved_locations)),
        "is_conflict": is_conflict,
        "intent": intent.value if intent else "UNKNOWN",
        "confidence": round(result.confidence, 2),
        "entities": _serialize_entities(entities),
        "api_endpoint": real_endpoint,
        "api_body": real_body
    }

def run_conflict_simulation(loader) -> list:
    """Reads the test suite from the txt file and runs the batch simulation."""
    vocab = set()
    if os.path.exists(TEST_FILE_PATH):
        with open(TEST_FILE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                clean_line = line.strip()
                if clean_line and not clean_line.startswith("#"):
                    vocab.add(clean_line)
    else:
        open(TEST_FILE_PATH, 'a').close()

    conflicts = []
    if not vocab: return conflicts

    for term in vocab:
        result = simulate_single_term(term)
        if result["is_conflict"]:
            conflicts.append({
                "term": term,
                "locations": result["locations"]
            })

    conflicts.sort(key=lambda x: x.get('term', ''))
    return conflicts

def get_saved_queries() -> list:
    """Helper to return the list of currently saved questions to the dashboard."""
    if not os.path.exists(TEST_FILE_PATH): return []
    with open(TEST_FILE_PATH, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]