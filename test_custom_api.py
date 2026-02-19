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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_finish"
        assert filters[0]["terms"] == "matte"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_tile-size"
        assert filters[0]["terms"] == "24x48"  # Quotes stripped
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_colors"
        assert filters[0]["terms"] == "grey"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_thickness"
        assert filters[0]["terms"] == "3/8"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_edge"
        assert filters[0]["terms"] == "rectified"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_application"
        assert filters[0]["terms"] == "floor"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_visual"
        assert filters[0]["terms"] == "marble"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_visual"
        assert filters[0]["terms"] == "wood"
        assert api_calls[0].params["page"] == 1

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
        
        import json
        filters = json.loads(api_calls[0].params["filters"])
        assert filters[0]["attribute"] == "pa_origin"
        assert filters[0]["terms"] == "italy"
        assert api_calls[0].params["page"] == 1
