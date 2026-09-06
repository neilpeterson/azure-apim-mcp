"""Tests for the list envelope, renderers, and truncation (T-04). See docs/SPEC.md §6.0."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from apim_mcp.common.formatting import (
    apply_truncation,
    build_list_envelope,
    render_json,
    render_markdown,
)


def _sample_items() -> list[dict[str, Any]]:
    return [
        {"id": "orders-api", "displayName": "Orders API", "path": "orders"},
        {"id": "billing-api", "displayName": "Billing API", "path": "billing"},
    ]


def test_build_list_envelope_shape_with_more_pages() -> None:
    envelope = build_list_envelope(_sample_items(), total=50, offset=0, limit=25)
    assert envelope["total"] == 50
    assert envelope["count"] == 2
    assert envelope["offset"] == 0
    assert envelope["items"] == _sample_items()
    assert envelope["has_more"] is True
    assert envelope["next_offset"] == 2


def test_build_list_envelope_no_more_pages() -> None:
    envelope = build_list_envelope([{"id": "a"}], total=1, offset=0, limit=25)
    assert envelope["has_more"] is False
    assert envelope["next_offset"] is None


def test_json_render_round_trips_to_same_data() -> None:
    envelope = build_list_envelope(_sample_items(), total=2, offset=0, limit=25)
    assert json.loads(render_json(envelope)) == envelope


def test_markdown_and_json_are_equivalent() -> None:
    """Both renderers must carry the same information for the same input."""
    envelope = build_list_envelope(_sample_items(), total=50, offset=0, limit=25)
    json_data: Mapping[str, Any] = json.loads(render_json(envelope))
    markdown = render_markdown(envelope)

    for key, value in json_data.items():
        if key == "items" or value is None:
            continue
        expected = "true" if value is True else "false" if value is False else str(value)
        assert expected in markdown, f"{key}={value!r} missing from markdown"

    for item in json_data["items"]:
        for value in item.values():
            assert str(value) in markdown, f"{value!r} missing from markdown"


def test_truncation_noop_when_under_budget() -> None:
    envelope = build_list_envelope([{"id": "a"}], total=1, offset=0, limit=25)
    result = apply_truncation(envelope, max_bytes=48_000, narrow_param="limit")
    assert result["truncated"] is False
    assert result["items"] == [{"id": "a"}]


def test_truncation_sets_hint() -> None:
    items = [{"id": f"api-{i}", "description": "x" * 200} for i in range(500)]
    envelope = build_list_envelope(items, total=500, offset=0, limit=500)

    truncated = apply_truncation(envelope, max_bytes=2_000, narrow_param="limit")

    assert truncated["truncated"] is True
    assert "hint" in truncated
    assert "'limit'" in truncated["hint"]
    assert len(truncated["items"]) < len(items)
    assert len(render_json(truncated).encode("utf-8")) <= 2_000


def test_truncation_updates_pagination_fields() -> None:
    items = [{"id": f"api-{i}", "description": "x" * 200} for i in range(500)]
    envelope = build_list_envelope(items, total=500, offset=0, limit=500)

    truncated = apply_truncation(envelope, max_bytes=2_000, narrow_param="limit")

    assert truncated["count"] == len(truncated["items"])
    assert truncated["has_more"] is True
    assert truncated["next_offset"] == len(truncated["items"])
