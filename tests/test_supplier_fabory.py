import asyncio
import unittest

from browser.supplier_fabory import _is_logged_in, _is_search_outcome_url


class _FakeLocator:
    def __init__(self, *, text="", count=0):
        self._text = text
        self._count = count

    async def inner_text(self):
        return self._text

    async def count(self):
        return self._count


class _FakePage:
    def __init__(self, *, logout_count, logged_marker_count, search_count):
        self._counts = {
            "a[href$='/logout']": logout_count,
            ".user-logged, .logged_in": logged_marker_count,
            "#search": search_count,
        }

    def locator(self, selector):
        return _FakeLocator(count=self._counts.get(selector, 0))


class FaboryAuthenticationTests(unittest.TestCase):
    def test_authenticated_account_requires_account_markers_and_search(self):
        page = _FakePage(logout_count=1, logged_marker_count=1, search_count=1)
        self.assertTrue(asyncio.run(_is_logged_in(page)))

    def test_guest_homepage_is_not_authenticated(self):
        page = _FakePage(logout_count=0, logged_marker_count=0, search_count=1)
        self.assertFalse(asyncio.run(_is_logged_in(page)))


class FaborySearchStateTests(unittest.TestCase):
    def test_product_page_is_a_search_outcome(self):
        self.assertTrue(
            _is_search_outcome_url("https://www.fabory.com/hu/example/p/51080100001")
        )

    def test_search_results_page_is_a_search_outcome(self):
        self.assertTrue(
            _is_search_outcome_url(
                "https://www.fabory.com/hu/search/?text=51080.100.001"
            )
        )

    def test_homepage_is_not_a_search_outcome(self):
        self.assertFalse(_is_search_outcome_url("https://www.fabory.com/hu"))


if __name__ == "__main__":
    unittest.main()
