"""
Registry of all known tags, attributes, and product names from WGC store.
"""

from typing import Optional, Dict

_store_loader = None


def set_store_loader(loader):
    global _store_loader
    _store_loader = loader


def get_store_loader():
    return _store_loader


# ─── STATIC TAGS ───
TAGS = {
    "2022-collection":     {"id": 1157, "name": "2022 Collection"},
    "2023-collection":     {"id": 1962, "name": "2023 Collection"},
    "2025-collection":     {"id": 58,   "name": "2025 collection"},
    "beige-tones":         {"id": 1159, "name": "Beige Tones"},
    "black-tones":         {"id": 1153, "name": "Black Tones"},
    "brown-tones":         {"id": 66,   "name": "brown tones"},
    "gray-tones":          {"id": 1152, "name": "Gray Tones"},
    "grey-tones":          {"id": 1969, "name": "Grey Tones"},
    "multi-tones":         {"id": 1160, "name": "Multi Tones"},
    "taupe-tones":         {"id": 1169, "name": "Taupe Tones"},
    "white-tones":         {"id": 1151, "name": "White Tones"},
    "matte-finish":        {"id": 65,   "name": "Matte finish"},
    "polished-finish":     {"id": 1187, "name": "Polished Finish"},
    "rectified-edge":      {"id": 63,   "name": "rectified edge"},
    "6-5mm-thick":         {"id": 1165, "name": "6.5mm Thick"},
    "10mm-thick":          {"id": 2527, "name": "10mm Thick"},
    "12mm-thick":          {"id": 1804, "name": "12mm Thick"},
    "11-32-thick":         {"id": 1177, "name": '11/32" Thick'},
    "7-16-thick":          {"id": 73,   "name": '7/16" thick'},
    "stone-look":          {"id": 67,   "name": "stone look"},
    "marble-look":         {"id": 1201, "name": "Marble Look"},
    "mosaic-look":         {"id": 1162, "name": "Mosaic Look"},
    "terrazzo-look":       {"id": 1178, "name": "Terrazzo Look"},
    "gauge-panel-look":    {"id": 60,   "name": "gauge panel look"},
    "shapes-patterns-decor-look": {"id": 1150, "name": "Shapes / Patterns / Decor Look"},
    "made-in-italy":       {"id": 69,   "name": "Made in Italy"},
    "made-in-turkey":      {"id": 1176, "name": "Made in Turkey"},
    "quick-ship":          {"id": 1154, "name": "Quick Ship"},
    "chip-card":           {"id": 48,   "name": "Chip Card"},
    "ymal":                {"id": 45,   "name": "YMAL"},
    "v2-variation":        {"id": 1158, "name": "V2 Variation"},
    "v3-variation":        {"id": 1179, "name": "V3 Variation"},
    "affogato-series":     {"id": 2157, "name": "Affogato Series"},
    "affogato-mosaic":     {"id": 2158, "name": "Affogato Mosaic"},
    "akard-series":        {"id": 1180, "name": "Akard Series"},
    "akard-mosaic":        {"id": 1196, "name": "Akard Mosaic"},
}

ATTRIBUTES = {
    "pa_visual":          {"id": 1,  "name": "Visual"},
    "pa_quick-ship":      {"id": 2,  "name": "Quick Ship"},
    "pa_pricing":         {"id": 3,  "name": "Pricing"},
    "pa_finish":          {"id": 4,  "name": "Finish"},
    "pa_tile-size":       {"id": 5,  "name": "Tile Size"},
    "pa_edge":            {"id": 6,  "name": "Edge"},
    "pa_thickness":       {"id": 7,  "name": "Thickness"},
    "pa_trim":            {"id": 8,  "name": "Trim"},
    "pa_application":     {"id": 9,  "name": "Application"},
    "pa_collection-year": {"id": 10, "name": "Collection Year"},
    "pa_colors":          {"id": 11, "name": "Colors"},
    "pa_colors-2":        {"id": 12, "name": "Colors 2"},
    "pa_origin":          {"id": 13, "name": "Origin"},
    "pa_sample-size":     {"id": 14, "name": "Sample Size"},
    "pa_chip-size":       {"id": 15, "name": "Chip Size"},
    "pa_variation":       {"id": 16, "name": "Variation"},
}

# ★ UPDATED: includes all product series from your store data
PRODUCT_SERIES = [
    "adams", "affogato", "akard", "allspice", "ansel",
    "cairo", "cord", "divine", "waterfall",
]

COLOR_MAP = {
    "white": "white-tones", "grey": "grey-tones", "gray": "gray-tones",
    "beige": "beige-tones", "black": "black-tones", "brown": "brown-tones",
    "taupe": "taupe-tones", "multi": "multi-tones",
}

FINISH_MAP = {
    "matte": "matte-finish", "matt": "matte-finish",
    "polished": "polished-finish", "glossy": "polished-finish",
    "honed": "matte-finish",
}

VISUAL_MAP = {
    "stone": "stone-look", "marble": "marble-look", "mosaic": "mosaic-look",
    "terrazzo": "terrazzo-look", "gauge": "gauge-panel-look",
    "pattern": "shapes-patterns-decor-look", "decor": "shapes-patterns-decor-look",
    "shape": "shapes-patterns-decor-look",
}

ORIGIN_MAP = {
    "italy": "made-in-italy", "italian": "made-in-italy",
    "turkey": "made-in-turkey", "turkish": "made-in-turkey",
}

SIZE_KEYWORD_MAP = {
    "24x48": '24"x48"', "48x48": '48"x48"', "48x110": '48"x110"',
    "32x32": '32"x32"', "12x24": "12x24",
    "large format": '48"x48"', "large": '48"x48"', "small": "12x24",
}

THICKNESS_MAP = {
    "6.5mm": "6-5mm-thick", "6.5": "6-5mm-thick",
    "10mm": "10mm-thick", "12mm": "12mm-thick",
    "11/32": "11-32-thick", "7/16": "7-16-thick",
}