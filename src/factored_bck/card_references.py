"""Bounded explicit card references shared by the trusted host and intent policy."""

import re

PRODUCT_ID = re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|card\d+[A-Za-z0-9_-]*)\b")
MAX_ID_LENGTH = 100


def card_ids(text, known=()):
    """Recognize opaque ID tokens and exact known IDs; never establish ownership."""
    found = [
        (match.start(), match.group())
        for match in PRODUCT_ID.finditer(text)
        if len(match.group()) <= MAX_ID_LENGTH
    ]
    for product in known:
        pattern = rf"(?<![\w-]){re.escape(product)}(?![\w-])"
        found.extend((match.start(), product) for match in re.finditer(pattern, text))
    return list(dict.fromkeys(product for _, product in sorted(found)))
