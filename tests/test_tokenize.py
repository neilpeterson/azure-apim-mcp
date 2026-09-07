"""Tests for docs/SPEC.md §7.3 tokenization."""

from __future__ import annotations

import pytest

from apim_mcp.index.tokenize import tokenize


def test_camel_case_split() -> None:
    assert tokenize("getInventoryLevels") == ["get", "inventory", "levels"]


def test_url_path_segments_split() -> None:
    assert tokenize("/orders/{orderId}/line-items") == [
        "orders",
        "order",
        "id",
        "line",
        "items",
    ]


def test_snake_case_split_with_acronym() -> None:
    assert tokenize("SKU_count") == ["sku", "count"]


def test_acronym_followed_by_capitalized_word() -> None:
    assert tokenize("parseXMLResponse") == ["parse", "xml", "response"]


def test_idempotent_on_already_tokenized_input() -> None:
    tokens = tokenize("getInventoryLevels")
    assert tokenize(" ".join(tokens)) == tokens


@pytest.mark.parametrize(
    "text",
    [
        "getInventoryLevels",
        "/orders/{orderId}/line-items",
        "SKU_count",
        "parseXMLResponse",
        "already lower case words",
    ],
)
def test_idempotent_parametrized(text: str) -> None:
    once = tokenize(text)
    twice = tokenize(" ".join(once))
    assert once == twice


def test_pascal_case_split() -> None:
    assert tokenize("GetInventoryLevels") == ["get", "inventory", "levels"]


def test_kebab_case_split() -> None:
    assert tokenize("line-items-report") == ["line", "items", "report"]


def test_empty_and_none_input() -> None:
    assert tokenize("") == []
    assert tokenize(None) == []


def test_preserves_repetition_for_bm25_weighting() -> None:
    # §7.3 repeats fields for weighting; tokenize must not dedupe.
    repeated = " ".join(["orders"] * 3)
    assert tokenize(repeated) == ["orders", "orders", "orders"]


def test_mixed_separators() -> None:
    assert tokenize("Ocp-Apim-Subscription-Key") == [
        "ocp",
        "apim",
        "subscription",
        "key",
    ]
