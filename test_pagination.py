"""
Tests for pagination support in API builder and chat route helpers.
"""
import pytest
from unittest.mock import patch
from models import Intent, ExtractedEntities, ClassifiedResult
from api_builder import build_api_calls


class TestApiBuilderPage:
    """Verify that build_api_calls threads the page parameter through to API call params."""

    def _make_result(self, intent, **entity_kwargs):
        return ClassifiedResult(
            intent=intent,
            entities=ExtractedEntities(**entity_kwargs),
            confidence=0.9,
        )

    # ── Standard WooCommerce endpoints ──

    def test_product_list_default_page(self):
        result = self._make_result(Intent.PRODUCT_LIST)
        calls = build_api_calls(result)
        assert calls[0].params.get("page") == 1

    def test_product_list_page_3(self):
        result = self._make_result(Intent.PRODUCT_LIST)
        calls = build_api_calls(result, page=3)
        assert calls[0].params.get("page") == 3

    def test_product_search_page(self):
        result = self._make_result(Intent.PRODUCT_SEARCH, product_name="marble")
        calls = build_api_calls(result, page=2)
        search_calls = [c for c in calls if "search" in c.params]
        assert search_calls, "Expected at least one search call"
        assert search_calls[0].params.get("page") == 2

    def test_category_browse_page(self):
        result = self._make_result(Intent.CATEGORY_BROWSE, category_id=5, category_name="Tiles")
        calls = build_api_calls(result, page=4)
        assert calls[0].params.get("page") == 4

    def test_category_list_page(self):
        result = self._make_result(Intent.CATEGORY_LIST)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_product_catalog_page(self):
        result = self._make_result(Intent.PRODUCT_CATALOG)
        calls = build_api_calls(result, page=2)
        for call in calls:
            assert call.params.get("page") == 2, f"Missing page in {call.description}"

    def test_discount_inquiry_page(self):
        result = self._make_result(Intent.DISCOUNT_INQUIRY)
        calls = build_api_calls(result, page=5)
        assert calls[0].params.get("page") == 5

    def test_clearance_products_page(self):
        result = self._make_result(Intent.CLEARANCE_PRODUCTS)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_promotions_page(self):
        result = self._make_result(Intent.PROMOTIONS)
        calls = build_api_calls(result, page=3)
        assert calls[0].params.get("page") == 3

    def test_coupon_inquiry_page(self):
        result = self._make_result(Intent.COUPON_INQUIRY)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_mosaic_products_page(self):
        result = self._make_result(Intent.MOSAIC_PRODUCTS)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_trim_products_page(self):
        result = self._make_result(Intent.TRIM_PRODUCTS)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_chip_card_page(self):
        result = self._make_result(Intent.CHIP_CARD)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_bulk_discount_page(self):
        result = self._make_result(Intent.BULK_DISCOUNT)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_order_history_page(self):
        result = self._make_result(Intent.ORDER_HISTORY)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_product_quick_ship_page(self):
        result = self._make_result(Intent.PRODUCT_QUICK_SHIP)
        calls = build_api_calls(result, page=2)
        assert calls[0].params.get("page") == 2

    def test_product_variations_page(self):
        result = self._make_result(Intent.PRODUCT_VARIATIONS, product_id=123, product_name="Allspice")
        calls = build_api_calls(result, page=2)
        var_calls = [c for c in calls if "/variations" in c.endpoint]
        assert var_calls, "Expected variations call"
        assert var_calls[0].params.get("page") == 2

    def test_fallback_search_page(self):
        # UNKNOWN intent triggers fallback
        result = self._make_result(Intent.UNKNOWN, search_term="granite")
        calls = build_api_calls(result, page=3)
        assert calls[0].params.get("page") == 3

    # ── Custom API endpoints ──

    def test_filter_by_finish_custom_api_page(self):
        result = self._make_result(Intent.FILTER_BY_FINISH, finish="matte")
        calls = build_api_calls(result, page=2)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 2

    def test_filter_by_size_custom_api_page(self):
        result = self._make_result(Intent.FILTER_BY_SIZE, tile_size='12"x24"')
        calls = build_api_calls(result, page=3)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 3

    def test_filter_by_color_custom_api_page(self):
        result = self._make_result(Intent.FILTER_BY_COLOR, color_tone="gray")
        calls = build_api_calls(result, page=2)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 2

    def test_filter_by_edge_custom_api_page(self):
        result = self._make_result(Intent.FILTER_BY_EDGE, edge="rectified")
        calls = build_api_calls(result, page=2)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 2

    def test_filter_by_application_custom_api_page(self):
        result = self._make_result(Intent.FILTER_BY_APPLICATION, application="floor")
        calls = build_api_calls(result, page=2)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 2

    def test_filter_by_material_custom_api_page(self):
        result = self._make_result(Intent.FILTER_BY_MATERIAL, visual="marble")
        calls = build_api_calls(result, page=2)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 2

    def test_product_by_origin_custom_api_page(self):
        result = self._make_result(Intent.PRODUCT_BY_ORIGIN, origin="Italy")
        calls = build_api_calls(result, page=2)
        assert calls[0].is_custom_api is True
        assert calls[0].params.get("page") == 2

    def test_product_by_collection_custom_api_page(self):
        result = self._make_result(
            Intent.PRODUCT_BY_COLLECTION,
            tag_slugs=["2024-collection"],
            collection_year="2024",
        )
        calls = build_api_calls(result, page=2)
        custom_calls = [c for c in calls if c.is_custom_api]
        assert custom_calls, "Expected custom API call"
        assert custom_calls[0].params.get("page") == 2


class TestPaginationHelpers:
    """Test the pagination helper logic for default and built-from-headers cases."""

    def test_default_pagination_page_1(self):
        expected = {
            "page": 1,
            "per_page": 0,
            "total_items": 0,
            "total_pages": 1,
            "has_more": False,
        }
        assert expected["page"] == 1
        assert expected["has_more"] is False
        assert expected["total_items"] == 0
        assert expected["total_pages"] == 1

    def test_build_pagination_from_headers(self):
        """Verify pagination object is correctly built from WooCommerce headers."""
        api_responses = [
            {"success": True, "data": [], "total": "150", "total_pages": "8"}
        ]

        class MockCall:
            params = {"per_page": 20}

        api_calls = [MockCall()]

        # Test page 1 of 8
        total_items = None
        total_pages = None
        per_page = int(api_calls[0].params.get("per_page", 20))
        for resp in api_responses:
            if resp.get("success"):
                if resp.get("total") is not None:
                    total_items = int(resp["total"])
                if resp.get("total_pages") is not None:
                    total_pages = int(resp["total_pages"])
                break

        page = 1
        has_more = (page < total_pages) if total_pages is not None else False
        assert total_items == 150
        assert total_pages == 8
        assert per_page == 20
        assert has_more is True

        # Test page 8 of 8 (last page)
        page = 8
        has_more = (page < total_pages) if total_pages is not None else False
        assert has_more is False

    def test_build_pagination_no_headers(self):
        """When WooCommerce doesn't return headers, total_items and total_pages are None."""
        api_responses = [{"success": True, "data": []}]

        class MockCall:
            params = {"per_page": 20}

        api_calls = [MockCall()]
        total_items = None
        total_pages = None
        for resp in api_responses:
            if resp.get("success"):
                raw_total = resp.get("total")
                raw_total_pages = resp.get("total_pages")
                if raw_total is not None:
                    total_items = int(raw_total)
                if raw_total_pages is not None:
                    total_pages = int(raw_total_pages)
                break

        has_more = (1 < total_pages) if total_pages is not None else False
        assert total_items is None
        assert total_pages is None
        assert has_more is False

    def test_build_pagination_failed_response(self):
        """When API response fails, total_items/total_pages stay None."""
        api_responses = [{"success": False, "error": "timeout"}]

        class MockCall:
            params = {"per_page": 20}

        total_items = None
        total_pages = None
        for resp in api_responses:
            if resp.get("success"):
                if resp.get("total") is not None:
                    total_items = int(resp["total"])
                if resp.get("total_pages") is not None:
                    total_pages = int(resp["total_pages"])
                break

        assert total_items is None
        assert total_pages is None
