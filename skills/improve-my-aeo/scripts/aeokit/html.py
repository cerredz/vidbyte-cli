"""Reads the facts an answer engine sees in raw HTML: head tags, headings, links, JSON-LD, and visible text."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

SKIP_TEXT = {"script", "style", "noscript", "template", "svg"}
FRAMEWORK_MARKERS = (
    ("next.js", re.compile(r"__NEXT_DATA__|/_next/static/")),
    ("nuxt", re.compile(r"__NUXT__|/_nuxt/")),
    ("gatsby", re.compile(r"___gatsby")),
    ("react-root", re.compile(r"<div id=\"(root|app)\"\s*>\s*</div>", re.I)),
    ("wordpress", re.compile(r"/wp-content/|/wp-includes/")),
    ("webflow", re.compile(r"data-wf-page|webflow\.js", re.I)),
    ("shopify", re.compile(r"cdn\.shopify\.com")),
    ("wix", re.compile(r"static\.wixstatic\.com|wix-warmup-data")),
    ("squarespace", re.compile(r"static1\.squarespace\.com")),
    ("framer", re.compile(r"framerusercontent\.com|data-framer-")),
)


class PageExtractor(HTMLParser):
    """An HTMLParser that keeps only what the audit checks; call facts() after feed()."""

    def __init__(self) -> None:
        # convert_charrefs makes text arrive already unescaped.
        super().__init__(convert_charrefs=True)
        self.title_parts: list = []
        self.meta: dict = {}
        self.links: list = []
        self.anchors: list = []
        self.headings: list = []
        self.jsonld_raw: list = []
        self.times: list = []
        self.text_parts: list = []
        self._stack: list = []
        self._heading = None
        self._script_type = None
        self._script_parts: list = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        # Records head tags, anchors, time elements, and opens text capture for headings and JSON-LD.
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TEXT:
            self._stack.append(tag)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if name and name not in self.meta:
                self.meta[name] = a.get("content", "")
        elif tag == "link":
            self.links.append({"rel": a.get("rel", "").lower(), "href": a.get("href", ""), "type": a.get("type", "").lower(), "hreflang": a.get("hreflang", "")})
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"])
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag in ("h1", "h2", "h3"):
            self._heading = [tag, []]
        if tag == "script":
            self._script_type = a.get("type", "").lower()
            self._script_parts = []

    def handle_endtag(self, tag: str) -> None:
        # Closes captures and stores finished headings and JSON-LD blocks.
        if tag == "title":
            self._in_title = False
        if tag in ("h1", "h2", "h3") and self._heading and self._heading[0] == tag:
            text = " ".join("".join(self._heading[1]).split())
            if text:
                self.headings.append((tag, text))
            self._heading = None
        if tag == "script":
            if self._script_type == "application/ld+json":
                self.jsonld_raw.append("".join(self._script_parts))
            self._script_type = None
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        # Routes text to the title, the script buffer, the open heading, or the visible text.
        if self._script_type is not None:
            self._script_parts.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._stack:
            return
        if self._heading is not None:
            self._heading[1].append(data)
        self.text_parts.append(data)

    def facts(self, raw_html: str = "") -> dict:
        # The page's facts as plain data; raw_html adds framework markers.
        text = " ".join(" ".join(self.text_parts).split())
        nodes, errors = JsonLd.parse_all(self.jsonld_raw)
        canonical = next((item["href"] for item in self.links if "canonical" in item["rel"].split()), None)
        alternates = [{"type": item["type"], "href": item["href"], "hreflang": item["hreflang"]} for item in self.links if "alternate" in item["rel"].split()]
        return {
            "title": " ".join("".join(self.title_parts).split()) or None,
            "description": self.meta.get("description") or None,
            "robots_meta": " ".join(v for k, v in self.meta.items() if k in ("robots", "googlebot", "bingbot", "applebot")) or None,
            "meta_names": sorted(self.meta),
            "og_site_name": self.meta.get("og:site_name") or None,
            "og_title": self.meta.get("og:title") or None,
            "og_image": self.meta.get("og:image") or None,
            "canonical": canonical,
            "alternates": alternates,
            "feeds": [item["href"] for item in self.links if "alternate" in item["rel"].split() and item["type"] in ("application/rss+xml", "application/atom+xml")],
            "h1": [t for h, t in self.headings if h == "h1"],
            "h2": [t for h, t in self.headings if h == "h2"][:40],
            "h3_count": sum(1 for h, _ in self.headings if h == "h3"),
            "anchors": self.anchors,
            "jsonld": nodes,
            "jsonld_errors": errors,
            "times": self.times[:10],
            "words": len(text.split()),
            "text": text,
            "frameworks": [name for name, pattern in FRAMEWORK_MARKERS if raw_html and pattern.search(raw_html)],
        }

    @classmethod
    def extract(cls, raw_html: str) -> dict:
        # Parses raw_html and returns its facts; malformed markup never raises.
        parser = cls()
        try:
            parser.feed(raw_html or "")
            parser.close()
        except Exception:  # noqa: BLE001 - HTMLParser can raise on pathological input; partial facts are still useful.
            pass
        return parser.facts(raw_html or "")


class JsonLd:
    """Parses JSON-LD blocks into a flat list of typed nodes."""

    @staticmethod
    def parse_all(blocks: list):
        # Returns (nodes, errors); @graph arrays and top-level lists are flattened, parse errors kept.
        nodes: list = []
        errors: list = []
        for index, block in enumerate(blocks):
            text = block.strip()
            if not text:
                continue
            try:
                document = json.loads(text)
            except ValueError as error:
                errors.append("block %d: %s" % (index + 1, str(error)[:120]))
                continue
            JsonLd._collect(document, nodes)
        return nodes, errors

    @staticmethod
    def _collect(document, nodes: list) -> None:
        # Appends every dict with an @type, descending into @graph and lists.
        if isinstance(document, list):
            for item in document:
                JsonLd._collect(item, nodes)
        elif isinstance(document, dict):
            if "@graph" in document:
                JsonLd._collect(document["@graph"], nodes)
            if "@type" in document:
                nodes.append(document)

    @staticmethod
    def types(node: dict) -> list:
        # The node's @type as a list of strings.
        value = node.get("@type")
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]
