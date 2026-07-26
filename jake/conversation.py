"""Pure helpers for turning Jake's flat turn list into things to draw.

Kept free of GTK/GObject imports on purpose: this is the tricky bookkeeping
behind the memory thread, and it is the part worth unit-testing in isolation.
"""

from __future__ import annotations

from typing import Iterable


def exchanges(history: Iterable[dict]) -> list[tuple[str, str]]:
    """History as (your line, Jake's line) pairs, newest last.

    A user turn with no assistant answer yet (or two user turns in a row, if a
    question was interrupted) pairs with an empty reply, so nothing is dropped.
    """
    pairs: list[tuple[str, str]] = []
    question: str | None = None
    for turn in history:
        if turn.get("role") == "user":
            if question is not None:
                pairs.append((question, ""))
            question = turn.get("content", "")
        elif question is not None:
            pairs.append((question, turn.get("content", "")))
            question = None
    if question is not None:
        pairs.append((question, ""))
    return pairs
