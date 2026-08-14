import asyncio
import unittest

from browser.messages import MSG_NOT_FOUND
from browser.supplier_inoxmare import (
    MSG_SEARCH_UNSTABLE,
    _is_exact_result_url,
    _is_explicit_not_found_url,
    _search_and_parse,
)


PART_NO = "55881000002"


class _ValueLocator:
    @property
    def first(self):
        return self

    async def get_attribute(self, name):
        if name == "value":
            return "2.84"
        return None

    async def count(self):
        return 1

    async def inner_text(self):
        return "HEX. NUT DIN 934-M10-A2-70"


class _StockLocator:
    async def inner_text(self):
        return "308.250"


class _RowLocator:
    def __init__(self, exists):
        self._exists = exists

    async def count(self):
        return int(self._exists)

    def locator(self, selector):
        if selector.startswith("td.price-box"):
            return _ValueLocator()
        if selector == "td.stock":
            return _StockLocator()
        if selector == "td.descr p":
            return _ValueLocator()
        raise AssertionError(f"Unexpected row selector: {selector}")


class _SearchLocator:
    def __init__(self, page):
        self._page = page

    async def wait_for(self, timeout):
        return None

    async def fill(self, value):
        self._page.filled = value

    async def press(self, key):
        self._page.attempt += 1
        state = self._page.outcomes[self._page.attempt - 1]
        if state == "exact":
            self._page.url = f"https://www.inoxmare.com/en/product.html?art={PART_NO}"
        elif state == "not_found":
            self._page.url = (
                "https://www.inoxmare.com/en/catalogsearch/result/index/q/UNKNOWN/"
            )
        else:
            self._page.url = "https://www.inoxmare.com/en"


class _BodyLocator:
    async def inner_text(self):
        return "Welcome, Buyer Sign Out"


class _SearchPage:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.attempt = 0
        self.filled = ""
        self.url = "https://www.inoxmare.com/en"

    async def goto(self, url, wait_until, timeout):
        self.url = url

    async def wait_for_function(self, script, arg, timeout):
        return None

    def locator(self, selector):
        if selector == "#item-input:visible":
            return _SearchLocator(self)
        if selector == "#item-input":
            return _RowLocator(True)
        if selector == "body":
            return _BodyLocator()
        if selector == f'tr[id="{PART_NO}"]':
            return _RowLocator(self.outcomes[self.attempt - 1] == "exact")
        raise AssertionError(f"Unexpected page selector: {selector}")


async def _emit(_message):
    return None


class InoxmareSearchStateTests(unittest.TestCase):
    def test_exact_article_url_is_recognised(self):
        self.assertTrue(
            _is_exact_result_url(
                f"https://www.inoxmare.com/en/product.html?art={PART_NO}", PART_NO
            )
        )

    def test_general_catalog_search_is_explicit_not_found(self):
        self.assertTrue(
            _is_explicit_not_found_url(
                "https://www.inoxmare.com/en/catalogsearch/result/index/q/UNKNOWN/"
            )
        )

    def test_transient_miss_is_retried_and_recovers(self):
        page = _SearchPage(["not_found", "exact"])
        result = asyncio.run(_search_and_parse(page, PART_NO, _emit))
        self.assertEqual(result["price_raw"], 2.84)
        self.assertEqual(result["stock"], 308250)
        self.assertEqual(page.attempt, 2)

    def test_two_explicit_misses_are_not_found(self):
        page = _SearchPage(["not_found", "not_found"])
        with self.assertRaisesRegex(RuntimeError, MSG_NOT_FOUND):
            asyncio.run(_search_and_parse(page, PART_NO, _emit))

    def test_two_ambiguous_states_are_not_reported_as_not_found(self):
        page = _SearchPage(["ambiguous", "ambiguous"])
        with self.assertRaisesRegex(RuntimeError, MSG_SEARCH_UNSTABLE):
            asyncio.run(_search_and_parse(page, PART_NO, _emit))

    def test_one_explicit_miss_is_not_enough_to_report_not_found(self):
        page = _SearchPage(["ambiguous", "not_found"])
        with self.assertRaisesRegex(RuntimeError, MSG_SEARCH_UNSTABLE):
            asyncio.run(_search_and_parse(page, PART_NO, _emit))


if __name__ == "__main__":
    unittest.main()
