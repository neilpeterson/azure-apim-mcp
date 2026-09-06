"""List envelope, markdown/JSON rendering, and size-based truncation.

See docs/SPEC.md §6.0 ("List response envelope", "Size ceiling").
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

ResponseFormat = Literal["markdown", "json"]


def build_list_envelope(
    items: Sequence[Mapping[str, Any]],
    *,
    total: int,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Build the standard list envelope: total/count/offset/items/has_more/next_offset."""
    count = len(items)
    has_more = offset + count < total
    envelope: dict[str, Any] = {
        "total": total,
        "count": count,
        "offset": offset,
        "items": list(items),
        "has_more": has_more,
    }
    if has_more:
        envelope["next_offset"] = offset + count
    else:
        envelope["next_offset"] = None
    del limit  # limit only affects how `items` was already sliced upstream
    return envelope


def _json_size(data: Mapping[str, Any]) -> int:
    return len(json.dumps(data, separators=(",", ":"), default=str).encode("utf-8"))


def apply_truncation(
    envelope: Mapping[str, Any],
    *,
    max_bytes: int,
    narrow_param: str,
) -> dict[str, Any]:
    """Shrink `items` until the envelope fits `max_bytes`.

    On truncation, sets ``truncated: true`` and a ``hint`` naming the
    parameter the caller should narrow, per §6.0's size ceiling.
    """
    data = dict(envelope)
    if _json_size(data) <= max_bytes:
        data.setdefault("truncated", False)
        return data

    data["truncated"] = True
    data["hint"] = (
        f"Response truncated to fit within the size limit. Narrow the '{narrow_param}' "
        "parameter to reduce the result size."
    )

    items = list(data.get("items", []))
    while items and _json_size({**data, "items": items}) > max_bytes:
        items.pop()

    data["items"] = items
    data["count"] = len(items)
    total = data.get("total")
    offset = data.get("offset", 0)
    if isinstance(total, int) and isinstance(offset, int):
        data["has_more"] = offset + len(items) < total
        data["next_offset"] = offset + len(items) if data["has_more"] else None
    return data


def render_json(data: Mapping[str, Any]) -> str:
    """Render a payload as compact JSON."""
    return json.dumps(data, separators=(",", ":"), default=str)


def _format_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_markdown(data: Mapping[str, Any]) -> str:
    """Render a payload as compact markdown.

    Every key/value present in `data` (including each item's fields) shows
    up as text, so :func:`render_markdown` and :func:`render_json` always
    carry the same information for the same input — see
    ``tests/test_formatting.py::test_markdown_and_json_are_equivalent``.
    """
    lines: list[str] = []
    items = data.get("items")
    meta = {k: v for k, v in data.items() if k != "items"}
    for key, value in meta.items():
        lines.append(f"- **{key}**: {_format_scalar(value)}")
    if isinstance(items, list):
        lines.append("")
        for index, item in enumerate(items):
            if isinstance(item, Mapping):
                fields = ", ".join(f"{k}={_format_scalar(v)}" for k, v in item.items())
                lines.append(f"{index + 1}. {fields}")
            else:
                lines.append(f"{index + 1}. {_format_scalar(item)}")
    return "\n".join(lines)


def render(data: Mapping[str, Any], response_format: ResponseFormat) -> str:
    """Render `data` in the requested format."""
    if response_format == "json":
        return render_json(data)
    return render_markdown(data)
