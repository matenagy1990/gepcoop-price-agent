import asyncio
import unittest

from playwright.async_api import TimeoutError as PlaywrightTimeout

from browser.messages import MSG_NOT_FOUND
from browser.supplier_kingb2b import (
    _SessionInvalid,
    _click_family_result,
    _is_semantically_empty_search,
    _is_king_search_response,
    _parse_eur,
    _parse_stock,
    _unstable_search_error,
)


def _search_state(**overrides):
    state = {
        "search_text_visible": True,
        "has_family": False,
        "old_row_count": 0,
        "article_table_rows": 0,
        "no_results_1": False,
        "no_results_2": False,
        "loading_text": False,
    }
    state.update(overrides)
    return state


class KingB2BSearchStateTests(unittest.TestCase):
    def test_query_with_empty_result_view_is_semantically_empty(self):
        self.assertTrue(_is_semantically_empty_search(_search_state()))

    def test_family_result_is_not_empty(self):
        self.assertFalse(
            _is_semantically_empty_search(_search_state(has_family=True))
        )

    def test_explicit_not_found_marker_is_not_ambiguous(self):
        self.assertFalse(
            _is_semantically_empty_search(_search_state(no_results_1=True))
        )

    def test_unconfirmed_query_is_workflow_failure_not_empty_result(self):
        self.assertFalse(
            _is_semantically_empty_search(
                _search_state(search_text_visible=False)
            )
        )

    def test_ambiguous_restored_session_requests_clean_reauthentication(self):
        error = _unstable_search_error(_search_state(), True)
        self.assertIsInstance(error, _SessionInvalid)

    def test_clean_session_empty_result_becomes_not_found(self):
        error = _unstable_search_error(_search_state(), False)
        self.assertEqual(str(error), MSG_NOT_FOUND)

    def test_clean_session_unconfirmed_query_keeps_workflow_error(self):
        error = _unstable_search_error(
            _search_state(search_text_visible=False),
            False,
        )
        self.assertIn("Search results did not stabilise", str(error))


class _CoveredFamilyRow:
    def __init__(self):
        self.click_timeout = None
        self.evaluated_script = None

    async def click(self, timeout):
        self.click_timeout = timeout
        raise PlaywrightTimeout("covered by portal overlay")

    async def evaluate(self, script):
        self.evaluated_script = script


class KingB2BFamilyClickTests(unittest.TestCase):
    def test_covered_family_uses_native_click_fallback(self):
        row = _CoveredFamilyRow()
        asyncio.run(_click_family_result(row))
        self.assertEqual(row.click_timeout, 5000)
        self.assertEqual(row.evaluated_script, "el => el.click()")


class _FakeRequest:
    def __init__(self, method, post_data):
        self.method = method
        self.post_data = post_data


class _FakeResponse:
    def __init__(self, url, method="POST", post_data=""):
        self.url = url
        self.request = _FakeRequest(method, post_data)


class KingB2BResponseTests(unittest.TestCase):
    def test_matches_search_rd3_response(self):
        response = _FakeResponse(
            "https://kingb2b.it/PORTAL/?WCI=RD3",
            post_data="cmd=eseguiRicerca&ricerca=2093410",
        )
        self.assertTrue(_is_king_search_response(response))

    def test_ignores_unrelated_rd3_response(self):
        response = _FakeResponse(
            "https://kingb2b.it/PORTAL/?WCI=RD3",
            post_data="cmd=ApriFamiglieArticoli",
        )
        self.assertFalse(_is_king_search_response(response))


class KingB2BParsingTests(unittest.TestCase):
    def test_parses_italian_euro_price(self):
        self.assertEqual(_parse_eur("2,98 %"), 2.98)

    def test_parses_italian_stock_number(self):
        self.assertEqual(_parse_stock("2.821.800"), 2_821_800)


if __name__ == "__main__":
    unittest.main()
