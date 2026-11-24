import time

import pytest

from toyshop.catalog import Catalog, Product
from toyshop.errors import UnknownProduct

N_PRODUCTS, N_TAGS, N_QUERIES, N_REPLACEMENTS = 50_000, 500, 20_000, 1_000
BUDGET_S = 5.0  # the indexed version needs well under a second; a full scan needs minutes


class CountingTags(frozenset):
    """Tags that count membership checks, to see whether a lookup scans every product."""

    checks = 0

    def __contains__(self, item):
        CountingTags.checks += 1
        return super().__contains__(item)


def make_catalog(n, n_tags, tag_type=frozenset):
    catalog = Catalog()
    expected: dict[str, set[str]] = {}
    for i in range(n):
        tags = {f"t{i % n_tags}", f"t{(i * 7) % n_tags}"}
        catalog.add(Product(f"P{i:06d}", f"Product {i}", 100 + i % 50, tag_type(tags)))
        for t in tags:
            expected.setdefault(t, set()).add(f"P{i:06d}")
    return catalog, expected


def test_by_tag_does_not_look_at_every_product():
    catalog, expected = make_catalog(10_000, 100, CountingTags)
    CountingTags.checks = 0
    for i in range(200):
        assert [p.sku for p in catalog.by_tag(f"t{i % 100}")] == sorted(expected[f"t{i % 100}"])
    # a full scan makes 10,000 checks per call (2,000,000 here); a lookup by tag needs none
    assert CountingTags.checks < 20_000, f"{CountingTags.checks} tag checks for 200 lookups"


def test_remove_and_replace_keep_tag_lookups_correct():
    catalog = Catalog()
    catalog.add(Product("A1", "Apple", 120, frozenset({"fruit", "red"})))
    catalog.add(Product("B2", "Banana", 45, frozenset({"fruit"})))
    catalog.add(Product("A1", "Apple", 130, frozenset({"red", "sale"})))  # replace: no longer "fruit"
    assert [p.sku for p in catalog.by_tag("fruit")] == ["B2"]
    assert [(p.sku, p.price_cents) for p in catalog.by_tag("sale")] == [("A1", 130)]

    catalog.remove("A1")
    assert len(catalog) == 1
    assert catalog.by_tag("red") == [] and catalog.by_tag("sale") == []
    assert catalog.search("apple") == []
    with pytest.raises(UnknownProduct):
        catalog.get("A1")
    with pytest.raises(UnknownProduct):
        catalog.remove("A1")
    catalog.add(Product("A1", "Apple", 120, frozenset({"fruit"})))  # can be added again
    assert [p.sku for p in catalog.by_tag("fruit")] == ["A1", "B2"]


def test_returned_list_cannot_change_the_catalog():
    catalog = Catalog()
    catalog.add(Product("A1", "Apple", 120, frozenset({"fruit"})))
    catalog.add(Product("B2", "Banana", 45, frozenset({"fruit"})))
    catalog.by_tag("fruit").clear()
    catalog.remove("B2")
    assert [p.sku for p in catalog.by_tag("fruit")] == ["A1"]


def test_by_tag_under_load_with_replacements():
    catalog, expected = make_catalog(N_PRODUCTS, N_TAGS)
    replace_every = N_QUERIES // N_REPLACEMENTS
    start = time.perf_counter()
    for i in range(N_QUERIES):
        if i % replace_every == 0:  # move one product to a different tag
            k = (i * 31) % N_PRODUCTS
            sku = f"P{k:06d}"
            for tags in expected.values():
                tags.discard(sku)
            new_tag = f"t{(i // replace_every) % N_TAGS}"
            catalog.add(Product(sku, f"Product {k}", 100, frozenset({new_tag})))
            expected[new_tag].add(sku)
        tag = f"t{(i * 13) % N_TAGS}"
        got = catalog.by_tag(tag)
        if i % 997 == 0:  # check a sample against the expected index
            assert [p.sku for p in got] == sorted(expected[tag])
        elapsed = time.perf_counter() - start
        if elapsed > BUDGET_S:
            pytest.fail(f"only {i} of {N_QUERIES} lookups done within {BUDGET_S}s: by_tag scans the catalog")
    assert len(catalog) == N_PRODUCTS
