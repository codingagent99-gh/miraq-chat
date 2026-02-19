"""
Test custom API integration for attribute filters.
"""
import pytest
from models import Intent, ClassifiedResult, ExtractedEntities
from api_builder import build_api_calls, CUSTOM_API_BASE


class TestCustomAPIIntegration:
    """Test that attribute filter intents use the custom API endpoint."""

    def test_filter_by_finish_uses_custom_api(self):
        """Test FILTER_BY_FINISH intent uses custom API."""
        entities = ExtractedEntities(finish="matte")
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_FINISH,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_finish"
        assert api_calls[0].params["term"] == "matte"

    def test_filter_by_size_uses_custom_api_and_strips_quotes(self):
        """Test FILTER_BY_SIZE intent uses custom API and strips quote characters."""
        entities = ExtractedEntities(tile_size='24"x48"')
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_SIZE,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_tile-size"
        assert api_calls[0].params["term"] == "24x48"  # Quotes stripped

    def test_filter_by_color_uses_custom_api(self):
        """Test FILTER_BY_COLOR intent uses custom API."""
        entities = ExtractedEntities(color_tone="grey")
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_COLOR,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_colors"
        assert api_calls[0].params["term"] == "grey"

    def test_filter_by_thickness_uses_custom_api(self):
        """Test FILTER_BY_THICKNESS intent uses custom API."""
        entities = ExtractedEntities(thickness="3/8")
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_THICKNESS,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_thickness"
        assert api_calls[0].params["term"] == "3/8"

    def test_filter_by_edge_uses_custom_api(self):
        """Test FILTER_BY_EDGE intent uses custom API."""
        entities = ExtractedEntities(edge="rectified")
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_EDGE,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_edge"
        assert api_calls[0].params["term"] == "rectified"

    def test_filter_by_application_uses_custom_api(self):
        """Test FILTER_BY_APPLICATION intent uses custom API."""
        entities = ExtractedEntities(application="floor")
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_APPLICATION,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_application"
        assert api_calls[0].params["term"] == "floor"

    def test_filter_by_material_uses_custom_api(self):
        """Test FILTER_BY_MATERIAL intent uses custom API with pa_visual."""
        entities = ExtractedEntities(visual="marble")
        result = ClassifiedResult(
            intent=Intent.FILTER_BY_MATERIAL,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_visual"
        assert api_calls[0].params["term"] == "marble"

    def test_product_by_visual_uses_custom_api(self):
        """Test PRODUCT_BY_VISUAL intent uses custom API."""
        entities = ExtractedEntities(visual="wood")
        result = ClassifiedResult(
            intent=Intent.PRODUCT_BY_VISUAL,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_visual"
        assert api_calls[0].params["term"] == "wood"

    def test_product_by_origin_uses_custom_api(self):
        """Test PRODUCT_BY_ORIGIN intent uses custom API."""
        entities = ExtractedEntities(origin="italy")
        result = ClassifiedResult(
            intent=Intent.PRODUCT_BY_ORIGIN,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 1
        assert CUSTOM_API_BASE in api_calls[0].endpoint
        assert "products-by-attribute" in api_calls[0].endpoint
        assert api_calls[0].params["attribute"] == "pa_origin"
        assert api_calls[0].params["term"] == "italy"

    def test_category_browse_with_attribute_uses_custom_api(self):
        """Test CATEGORY_BROWSE with attribute filter uses custom API for filtering."""
        entities = ExtractedEntities(
            category_id=10,
            category_name="Tiles",
            tile_size='12"x24"',
            attribute_slug="pa_tile-size",
            attribute_term_ids=[123, 456]
        )
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        # Should have 2 calls: category browse + attribute filter
        assert len(api_calls) == 2, f"Expected 2 API calls but got {len(api_calls)}"
        
        # First call: standard category browse
        category_call = api_calls[0]
        assert "/wc/v3/products" in category_call.endpoint
        assert category_call.params["category"] == "10"
        assert "attribute" not in category_call.params
        
        # Second call: custom API for attribute filtering
        attr_call = api_calls[1]
        assert CUSTOM_API_BASE in attr_call.endpoint
        assert "products-by-attribute" in attr_call.endpoint
        assert attr_call.params["attribute"] == "pa_tile-size"
        assert attr_call.params["term"] == "12x24"  # Quotes stripped
        assert attr_call.is_custom_api is True

    def test_category_browse_with_finish_uses_custom_api(self):
        """Test CATEGORY_BROWSE with finish filter uses custom API."""
        entities = ExtractedEntities(
            category_id=15,
            category_name="Wall Tiles",
            finish="matte",
            attribute_slug="pa_finish",
            attribute_term_ids=[789]
        )
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 2
        
        # First call: category
        assert "/wc/v3/products" in api_calls[0].endpoint
        assert api_calls[0].params["category"] == "15"
        
        # Second call: custom API for finish
        assert CUSTOM_API_BASE in api_calls[1].endpoint
        assert "products-by-attribute" in api_calls[1].endpoint
        assert api_calls[1].params["attribute"] == "pa_finish"
        assert api_calls[1].params["term"] == "matte"
        assert api_calls[1].is_custom_api is True

    def test_category_browse_with_color_uses_custom_api(self):
        """Test CATEGORY_BROWSE with color filter uses custom API."""
        entities = ExtractedEntities(
            category_id=20,
            category_name="Floor Tiles",
            color_tone="grey",
            attribute_slug="pa_colors",
            attribute_term_ids=[111]
        )
        result = ClassifiedResult(
            intent=Intent.CATEGORY_BROWSE,
            entities=entities,
            confidence=0.95
        )
        
        api_calls = build_api_calls(result)
        
        assert len(api_calls) == 2
        
        # Second call should use custom API for color
        attr_call = api_calls[1]
        assert CUSTOM_API_BASE in attr_call.endpoint
        assert "products-by-attribute" in attr_call.endpoint
        assert attr_call.params["attribute"] == "pa_colors"
        assert attr_call.params["term"] == "grey"
        assert attr_call.is_custom_api is True
