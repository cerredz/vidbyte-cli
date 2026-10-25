"""The evidence index: short observed facts, each tagged [E#], that checks and ideas cite."""

from __future__ import annotations

from collections import OrderedDict

MAX_TEXT = 300


class EvidenceIndex:
    """Hands out E1, E2, ... for observed facts and keeps them in order."""

    def __init__(self) -> None:
        # Tags map to {"kind", "url", "text"}; identical facts share one tag.
        self.items: "OrderedDict[str, dict]" = OrderedDict()
        self._by_key: dict = {}

    def add(self, kind: str, url: str, text: str) -> str:
        # Stores one fact (trimmed to MAX_TEXT characters, on one line) and returns its tag.
        line = " ".join(str(text).split())
        if len(line) > MAX_TEXT:
            line = line[: MAX_TEXT - 3] + "..."
        key = (kind, url or "", line)
        if key in self._by_key:
            return self._by_key[key]
        tag = "E%d" % (len(self.items) + 1)
        self.items[tag] = {"kind": kind, "url": url or "", "text": line}
        self._by_key[key] = tag
        return tag

    def to_json(self) -> dict:
        # The index as a plain dict for evidence.json.
        return dict(self.items)
