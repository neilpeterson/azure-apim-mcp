"""Tokenization for the API index, per docs/SPEC.md §7.3.

This one function is most of what makes `apim_search_apis` work: the BM25
corpus (§7.4) is built over these tokens, so a query for `inventory` has to
match an operation named `getInventoryLevels`. Every indexed field —
`operation_display_name`, `url_template`, `parameter_names`,
`schema_property_names`, `api_tags` — is tokenized with the same function
before being folded into `search_text`.
"""

from __future__ import annotations

import re

# Splits a run of non-alphanumeric characters: `/`, `{`, `}`, `-`, `_`,
# whitespace, and any other punctuation found in a URL template or
# identifier.
_SEPARATOR_RE = re.compile(r"[^0-9A-Za-z]+")

# Boundary between a lowercase letter/digit and an uppercase letter:
# "getInventory" -> "get Inventory".
_LOWER_TO_UPPER_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Boundary inside an acronym run, right before the last uppercase letter of
# the run when it starts a new capitalized word: "parseXMLResponse" splits
# into "parse", "XML", "Response" — the run "XMLR" breaks between "XML" and
# "Response", not before every capital.
_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def _split_camel(word: str) -> list[str]:
    spaced = _LOWER_TO_UPPER_RE.sub(" ", word)
    spaced = _ACRONYM_BOUNDARY_RE.sub(" ", spaced)
    return spaced.split()


def tokenize(text: str | None) -> list[str]:
    """Tokenize `text` into lowercase words.

    Splits on non-alphanumeric separators (path `/`, template `{`/`}`,
    `-`, `_`, whitespace, punctuation), then splits each resulting chunk on
    camelCase/PascalCase boundaries, keeping acronym runs intact
    (`parseXMLResponse` -> `parse`, `XML`, `Response`, not `X`, `M`, `L`,
    ...). Does not deduplicate: repeated tokens are preserved so field
    repetition (§7.3's weighting table) carries through to BM25 term
    frequency.

    Idempotent: tokenizing already-tokenized (lowercase, space-separated)
    input returns it unchanged.
    """
    if not text:
        return []

    tokens: list[str] = []
    for chunk in _SEPARATOR_RE.split(text):
        if not chunk:
            continue
        tokens.extend(part.lower() for part in _split_camel(chunk))
    return tokens
