"""Decides what to fetch from one site and records what came back, as plain data the checks read."""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .fetch import BROWSER_UA, FetchResult, HttpFetcher
from .html import PageExtractor
from .robots import RobotsTxt

# Real user-agent strings, so a firewall rule keyed on the token sees what the crawler would send.
# Requests still come from this machine's IP, so a pass does not prove IP-verified rules let the real crawler in.
UA_PROBES = (
    ("GPTBot", "training", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.4; +https://openai.com/gptbot"),
    ("OAI-SearchBot", "search", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36; compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot"),
    ("ChatGPT-User", "user", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("ClaudeBot", "training", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    ("Claude-SearchBot", "search", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-SearchBot/1.0; +Claude-SearchBot@anthropic.com)"),
    ("Claude-User", "user", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)"),
    ("PerplexityBot", "search", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
    ("Perplexity-User", "user", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"),
    ("bingbot", "search", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/141.0.0.0 Safari/537.36"),
    ("Googlebot", "search", "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
    ("Applebot", "search", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)"),
)

# robots.txt product tokens checked without a fetch (some, like Google-Extended, never fetch at all).
ROBOTS_TOKENS = (
    ("OAI-SearchBot", "search"), ("Claude-SearchBot", "search"), ("PerplexityBot", "search"), ("Googlebot", "search"),
    ("bingbot", "search"), ("Applebot", "search"), ("DuckAssistBot", "search"), ("meta-webindexer", "search"),
    ("Amzn-SearchBot", "search"), ("MistralAI-Index", "search"),
    ("ChatGPT-User", "user"), ("Claude-User", "user"), ("Perplexity-User", "user"), ("MistralAI-User", "user"),
    ("GPTBot", "training"), ("ClaudeBot", "training"), ("Google-Extended", "training"), ("Applebot-Extended", "training"),
    ("CCBot", "training"), ("meta-externalagent", "training"), ("Bytespider", "training"), ("Amazonbot", "training"),
    ("MistralAI-Training", "training"),
)

WELL_KNOWN = (
    "/.well-known/security.txt", "/.well-known/ard.json", "/.well-known/ai-catalog.json", "/.well-known/api-catalog",
    "/.well-known/mcp/server-card.json", "/.well-known/agent-skills/index.json", "/.well-known/skills/index.json",
)
CHALLENGE_RE = re.compile(r"Just a moment\.\.\.|challenge-platform|cf-chl-|captcha|Attention Required! \| Cloudflare|vercel-challenge|px-captcha|_Incapsula_", re.I)
INJECTION_RE = re.compile(r"ignore (all |any )?(previous|prior|above) instructions|(you|assistants?|models?|llms?|ai) (must|should) (always )?(recommend|rank|prefer|cite)|disregard (your|all) (instructions|guidelines)", re.I)
JSX_LINE_RE = re.compile(r"(?m)^\s*<(?:[A-Z][A-Za-z]+|div|span|script)\b")
PRIORITY_WORDS = ("pricing", "price", "plans", "about", "docs", "documentation", "features", "product", "faq", "compare", "vs", "alternative", "blog", "customers", "how-it-works")
ASSET_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|json|xml|gz|zip|pdf|mp4|webm|mp3|woff2?|ttf|txt|md)$", re.I)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+|/[^)\s]*)\)")
MAX_SITEMAP_FILES = 5
MAX_MD_PROBES = 10
MAX_LLMS_LINKS = 8
BODY_KEEP = 60_000


class MarkdownSniffer:
    """Tells Markdown from HTML in a response body."""

    @staticmethod
    def looks_markdown(result: FetchResult) -> bool:
        # True for a 2xx text/markdown body, or a text/plain body that starts like Markdown and holds no HTML document.
        if not result.ok or not result.body.strip():
            return False
        head = result.body.lstrip()[:400].lower()
        if head.startswith("<!doctype") or head.startswith("<html") or "<body" in head:
            return False
        if result.content_type in ("text/markdown", "text/x-markdown"):
            return True
        return result.content_type in ("text/plain", "") and bool(re.search(r"(?m)^(#{1,3} |\* |- |\d+\. |> )", result.body[:4000]))


class SiteProbe:
    """Fetches one site's machine-facing surfaces and a sample of its pages."""

    def __init__(self, fetcher: HttpFetcher, max_pages: int = 25) -> None:
        # max_pages bounds the HTML pages sampled, home page included.
        self.fetcher = fetcher
        self.max_pages = max(1, min(int(max_pages), 60))

    def run(self, url: str) -> dict:
        # Fetches everything for the site at url and returns the facts plus the parsed robots.txt.
        origin = self.origin(url)
        facts: dict = {"origin": origin, "host": urllib.parse.urlsplit(origin).hostname, "audited_at": dt.datetime.now().isoformat(timespec="seconds")}
        robots = self._robots(origin, facts)
        self._sitemaps(origin, robots, facts)
        self._llms(origin, facts)
        home = self._page(origin + "/", facts)
        facts["home"] = home
        self._ua_probes(origin, home, facts)
        self._negotiation(origin, facts)
        self._pages(origin, home, facts)
        self._mirrors(origin, facts)
        self._well_known(origin, facts)
        self._soft_404(origin, facts)
        facts["fetch_log"] = self.fetcher.log()
        return {"facts": facts, "robots": robots}

    def _soft_404(self, origin: str, facts: dict) -> None:
        # Requests a path that cannot exist; a real site answers 404 or 410, not 200 or a redirect home.
        path = "/aeo-audit-missing-%d" % (abs(hash(origin + facts["audited_at"])) % 10_000_000)
        result = self.fetcher.get(origin + path)
        facts["missing_page"] = {"url": origin + path, "status": result.status, "final_url": result.final_url, "redirects": len(result.redirects)}

    @staticmethod
    def origin(url: str) -> str:
        # Normalizes a user-typed site to scheme://host[:port]; refuses anything that is not http(s).
        text = url.strip()
        if "://" not in text:
            text = "https://" + text
        parts = urllib.parse.urlsplit(text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Not an http(s) site: %r" % url)
        return "%s://%s" % (parts.scheme, parts.netloc.lower())

    def _robots(self, origin: str, facts: dict) -> RobotsTxt:
        # Fetches robots.txt and records its status, shape, and the verdict for every AI token.
        result = self.fetcher.get(origin + "/robots.txt", accept="text/plain,*/*")
        robots = RobotsTxt(result.body, result.status, result.content_type)
        facts["robots"] = {
            "status": result.status, "content_type": result.content_type, "served_as_html": robots.served_as_html, "error": result.error,
            "bytes": result.bytes, "truncated": result.truncated,
            "text": result.body[:6000] if result.ok else "", "sitemaps": robots.sitemaps, "content_signals": robots.content_signals,
            "unknown_lines": robots.unknown_lines[:10], "lines": robots.lines,
            "tokens": [{"agent": a, "purpose": p, "named": robots.names(a), "home_allowed": robots.allowed(a, "/")} for a, p in ROBOTS_TOKENS],
        }
        return robots

    def _sitemaps(self, origin: str, robots: RobotsTxt, facts: dict) -> None:
        # Reads up to MAX_SITEMAP_FILES sitemap files (robots lines first, then defaults), one index level deep.
        queue = list(robots.sitemaps) or [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
        seen, files, urls = set(), [], []
        while queue and len(files) < MAX_SITEMAP_FILES:
            target = queue.pop(0)
            if target in seen:
                continue
            seen.add(target)
            if target.endswith(".gz"):
                files.append({"url": target, "status": None, "error": "gzip sitemap not read"})
                continue
            result = self.fetcher.get(target, accept="application/xml,text/xml,*/*")
            entry = {"url": target, "status": result.status, "error": result.error, "urls": 0, "lastmod": 0}
            locs, children = self._parse_sitemap(result.body) if result.ok else ([], [])
            entry.update(urls=len(locs), lastmod=sum(1 for _, lm in locs if lm), index=bool(children))
            files.append(entry)
            urls.extend(locs)
            queue.extend(c for c in children if c not in seen)
            if not robots.sitemaps and result.ok and locs:
                break
        facts["sitemaps"] = files
        facts["sitemap_urls"] = urls[:5000]

    @staticmethod
    def _parse_sitemap(body: str):
        # Returns ([(loc, lastmod)], [child sitemap urls]); namespace-agnostic and tolerant of bad XML.
        try:
            root = ET.fromstring(body.encode("utf-8"))
        except ET.ParseError:
            return [], []
        locs, children = [], []
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in ("url", "sitemap"):
                continue
            loc = lastmod = None
            for child in node:
                name = child.tag.rsplit("}", 1)[-1]
                if name == "loc":
                    loc = (child.text or "").strip()
                elif name == "lastmod":
                    lastmod = (child.text or "").strip()
            if loc:
                (locs if tag == "url" else children).append((loc, lastmod) if tag == "url" else loc)
        return locs, children

    def _llms(self, origin: str, facts: dict) -> None:
        # Fetches llms.txt and llms-full.txt and checks a sample of llms.txt's links.
        for name in ("llms", "llms_full"):
            path = "/llms.txt" if name == "llms" else "/llms-full.txt"
            result = self.fetcher.get(origin + path, accept="text/plain,text/markdown,*/*")
            is_text = result.ok and not result.body.lstrip()[:200].lower().startswith(("<!doctype", "<html"))
            facts[name] = {"url": origin + path, "status": result.status, "final_url": result.final_url, "content_type": result.content_type,
                           "bytes": result.bytes, "truncated": result.truncated, "content_length": result.header("content-length") or None,
                           "elapsed_ms": result.elapsed_ms, "is_text": is_text, "text": result.body[:BODY_KEEP] if is_text else "", "error": result.error,
                           "injection": [m.group(0) for m in INJECTION_RE.finditer(result.body[:BODY_KEEP])][:3] if is_text else []}
        facts["llms_links"] = self._llms_links(origin, facts["llms"]["text"])

    def _llms_links(self, origin: str, text: str) -> list:
        # Fetches up to MAX_LLMS_LINKS same-site links named in llms.txt and records their status.
        rows, seen = [], set()
        for match in MD_LINK_RE.finditer(text or ""):
            link = urllib.parse.urljoin(origin + "/", match.group(1))
            if link in seen or urllib.parse.urlsplit(link).netloc.lower() != urllib.parse.urlsplit(origin).netloc:
                continue
            seen.add(link)
            if len(rows) >= MAX_LLMS_LINKS:
                break
            result = self.fetcher.get(link, accept="text/markdown,text/plain,text/html,*/*")
            rows.append({"url": link, "status": result.status, "markdown": MarkdownSniffer.looks_markdown(result), "error": result.error})
        return rows

    def _page(self, url: str, facts: dict, user_agent: str = BROWSER_UA) -> dict:
        # Fetches one HTML page and returns its record: status, headers the audit reads, and extracted facts.
        result = self.fetcher.get(url, user_agent=user_agent)
        record = {"url": url, "status": result.status, "final_url": result.final_url, "content_type": result.content_type,
                  "x_robots_tag": result.header("x-robots-tag") or None, "link_header": result.header("link") or None,
                  "vary": result.header("vary") or None, "server": result.header("server") or None, "bytes": result.bytes, "truncated": result.truncated,
                  "redirects": len(result.redirects), "error": result.error, "challenge": bool(result.header("cf-mitigated")) or bool(CHALLENGE_RE.search(result.body[:20000]))}
        page = PageExtractor.extract(result.body) if result.ok and "html" in (result.content_type or "text/html") else PageExtractor.extract("")
        record["verification"] = sorted(k for k in ("msvalidate.01", "google-site-verification", "yandex-verification") if k in page.pop("meta_names", []))
        record["mentions_llms_txt"] = any(a.rstrip("/").endswith("llms.txt") for a in page.get("anchors", [])) or "llms" in (record["link_header"] or "").lower()
        netloc = urllib.parse.urlsplit(url).netloc.lower()
        record["internal_links"] = len({a for a in page.get("anchors", []) if a.startswith("/") and not a.startswith("//") or urllib.parse.urlsplit(a).netloc.lower() == netloc})
        text = page.pop("text")
        record.update(page)
        record["text_sample"] = text[:1500]
        record["_fulltext"] = text[:200_000]  # Underscore keys stay out of the digest.
        record["claims"] = ClaimFinder.find(text)
        return record

    def _ua_probes(self, origin: str, home: dict, facts: dict) -> None:
        # Fetches the home page once per AI user agent and records status, size, and words next to the browser's.
        rows = [{"agent": "browser", "purpose": "baseline", "status": home["status"], "bytes": home["bytes"], "words": home["words"], "error": home["error"]}]
        rows[0]["challenge"] = home.get("challenge", False)
        for agent, purpose, ua in UA_PROBES:
            result = self.fetcher.get(origin + "/", user_agent=ua)
            words = PageExtractor.extract(result.body)["words"] if result.ok else 0
            challenge = bool(result.header("cf-mitigated")) or bool(CHALLENGE_RE.search(result.body[:20000]))
            rows.append({"agent": agent, "purpose": purpose, "status": result.status, "bytes": result.bytes, "words": words, "challenge": challenge, "error": result.error})
        facts["ua_probes"] = rows

    def _negotiation(self, origin: str, facts: dict) -> None:
        # Asks for the home page with Accept: text/markdown (as Claude Code does) and records whether Markdown came back.
        result = self.fetcher.get(origin + "/", accept="text/markdown, text/html, */*")
        facts["negotiation"] = {"status": result.status, "content_type": result.content_type, "markdown": MarkdownSniffer.looks_markdown(result),
                                "vary": result.header("vary") or None, "link_header": result.header("link") or None, "bytes": result.bytes,
                                "content_signal": result.header("content-signal") or None, "markdown_tokens": result.header("x-markdown-tokens") or None}

    def _pages(self, origin: str, home: dict, facts: dict) -> None:
        # Samples up to max_pages - 1 more pages: priority paths, then sitemap order, then home-page links.
        candidates = PagePicker(origin).pick(home.get("anchors", []), [loc for loc, _ in facts.get("sitemap_urls", [])], self.max_pages - 1)
        pages = [home]
        seen = {home["final_url"].rstrip("/")}
        for url in candidates:
            page = self._page(url, facts)
            # /pricing and /pricing/ often land on one page; keep it once so titles and prices are not double-counted.
            if page["final_url"].rstrip("/") in seen:
                continue
            seen.add(page["final_url"].rstrip("/"))
            pages.append(page)
        for page in pages:
            page.pop("anchors", None)
        facts["pages"] = pages
        facts["sitemap_lastmod"] = {loc: lm for loc, lm in facts.get("sitemap_urls", []) if lm}

    def _mirrors(self, origin: str, facts: dict) -> None:
        # Looks for each sampled page's Markdown mirror: an advertised rel=alternate first, then the llms.txt URL conventions.
        for page in facts["pages"][:MAX_MD_PROBES]:
            if not page["status"] or page["status"] >= 400:
                continue
            advertised = [urllib.parse.urljoin(page["url"], a["href"]) for a in page.get("alternates", []) if a.get("type") in ("text/markdown", "text/x-markdown") and a.get("href")]
            mirror = None
            for candidate in advertised + MirrorUrls.candidates(page["url"]):
                result = self.fetcher.get(candidate, accept="text/markdown,text/plain,*/*")
                if MarkdownSniffer.looks_markdown(result):
                    mirror = {"url": candidate, "status": result.status, "content_type": result.content_type, "advertised": candidate in advertised,
                              "link_header": result.header("link") or None, "x_robots_tag": result.header("x-robots-tag") or None, "bytes": result.bytes,
                              "jsx_lines": len(JSX_LINE_RE.findall(result.body)), "first_line": result.body.lstrip()[:160].split("\n")[0]}
                    break
            page["md_mirror"] = mirror
            page["md_advertised"] = bool(advertised)

    def _well_known(self, origin: str, facts: dict) -> None:
        # Records the status of the discovery files agents look for.
        rows = []
        for path in WELL_KNOWN:
            result = self.fetcher.get(origin + path, accept="application/json,text/plain,*/*")
            html = result.body.lstrip()[:200].lower().startswith(("<!doctype", "<html"))
            rows.append({"path": path, "status": result.status, "content_type": result.content_type, "real": result.ok and not html,
                         "head": result.body[:3000] if result.ok and not html else ""})
        facts["well_known"] = rows


class MirrorUrls:
    """The Markdown mirror URLs llms.txt sites use for one HTML page."""

    @staticmethod
    def candidates(url: str) -> list:
        # "/" -> /index.html.md and /index.md; "/a/" -> /a/index.html.md and /a.md; "/a" -> /a.md.
        parts = urllib.parse.urlsplit(url)
        base = "%s://%s" % (parts.scheme, parts.netloc)
        path = parts.path or "/"
        if path.endswith(".md"):
            return []
        if path == "/":
            return [base + "/index.html.md", base + "/index.md"]
        if path.endswith("/"):
            return [base + path + "index.html.md", base + path.rstrip("/") + ".md"]
        return [base + path + ".md"]


class PagePicker:
    """Chooses which same-site pages to sample."""

    def __init__(self, origin: str) -> None:
        # Only URLs on origin's host are eligible.
        self.origin = origin
        self.netloc = urllib.parse.urlsplit(origin).netloc.lower()

    def pick(self, anchors: list, sitemap_locs: list, limit: int) -> list:
        # Priority-word pages first (from links, then the sitemap), then sitemap order, then links; no duplicates.
        if limit <= 0:
            return []
        links = [u for u in (self._clean(a) for a in anchors) if u]
        sitemap = [u for u in (self._clean(s) for s in sitemap_locs) if u]
        ordered = [u for u in links + sitemap if self._priority(u)] + sitemap + links
        chosen, seen = [], {self.origin + "/"}
        for url in ordered:
            if url not in seen:
                seen.add(url)
                chosen.append(url)
            if len(chosen) >= limit:
                break
        return chosen

    def _clean(self, href: str):
        # An absolute same-site page URL without fragment or query, or None for assets and other hosts.
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            return None
        absolute = urllib.parse.urljoin(self.origin + "/", href.strip())
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != self.netloc or ASSET_RE.search(parts.path):
            return None
        return "%s://%s%s" % (parts.scheme, parts.netloc.lower(), parts.path or "/")

    @staticmethod
    def _priority(url: str) -> bool:
        # True when a path segment names a page answer engines quote (pricing, about, docs, compare...).
        segments = re.split(r"[/_-]", urllib.parse.urlsplit(url).path.lower())
        return any(word in segments for word in PRIORITY_WORDS)


class ClaimFinder:
    """Pulls the facts that most often contradict each other across pages: prices, free claims, and status words."""

    PRICE_RE = re.compile(r"(?:[$€£]\s?\d[\d,]*(?:\.\d{1,2})?(?:\s?(?:/|per)\s?(?:mo|month|yr|year|user|seat|run|call))?)")
    FREE_RE = re.compile(r"[^.!?]{0,80}\b(free (?:forever|plan|tier|trial)|free to (?:start|use|try)|no credit card|100% free|always free)\b[^.!?]{0,60}", re.I)
    STATUS_RE = re.compile(r"[^.!?]{0,60}\b(beta|waitlist|coming soon|early access|invite[- ]only|generally available|now available|open source|open-source)\b[^.!?]{0,40}", re.I)

    @classmethod
    def find(cls, text: str) -> dict:
        # Up to 12 prices, 5 free claims, and 5 status phrases from a page's visible text.
        prices = list(dict.fromkeys(" ".join(m.split()) for m in cls.PRICE_RE.findall(text or "")))[:12]
        free = list(dict.fromkeys(" ".join(m.group(0).split()) for m in cls.FREE_RE.finditer(text or "")))[:5]
        status = list(dict.fromkeys(" ".join(m.group(0).split()) for m in cls.STATUS_RE.finditer(text or "")))[:5]
        return {"prices": prices, "free": free, "status": status}
