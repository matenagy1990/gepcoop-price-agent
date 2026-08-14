import asyncio
import unittest

from browser.supplier_wasishop import _is_authenticated, _parse_eur, _parse_stock


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, url: str, logout_count: int):
        self.url = url
        self.logout_count = logout_count
        self.selector = None

    def locator(self, selector: str) -> _FakeLocator:
        self.selector = selector
        return _FakeLocator(self.logout_count)


class WasishopAuthenticationTests(unittest.TestCase):
    def test_login_page_is_not_authenticated(self):
        page = _FakePage("https://www.wasishop.de/de/login_form.php", 1)
        self.assertFalse(asyncio.run(_is_authenticated(page)))

    def test_search_box_without_logout_is_not_authenticated(self):
        page = _FakePage("https://www.wasishop.de/de/", 0)
        self.assertFalse(asyncio.run(_is_authenticated(page)))

    def test_logout_marker_proves_authenticated_session(self):
        page = _FakePage("https://www.wasishop.de/de/", 1)
        self.assertTrue(asyncio.run(_is_authenticated(page)))
        self.assertIn("logout", page.selector)


class WasishopParsingTests(unittest.TestCase):
    def test_parses_german_euro_price(self):
        self.assertEqual(_parse_eur("3,05\u00a0€"), 3.05)

    def test_parses_german_stock_number(self):
        self.assertEqual(_parse_stock("4.477.800"), 4_477_800)


if __name__ == "__main__":
    unittest.main()
