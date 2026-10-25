"""The mechanical AEO checks: each reads the probe's facts and returns pass, fail, warn, info, or not_checked with evidence."""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from collections import Counter

from .evidence import EvidenceIndex
from .html import JsonLd
from .probe import CORE_BOTS
from .robots import RobotsTxt

PASS, FAIL, WARN, INFO, NOT_CHECKED = "pass", "fail", "warn", "info", "not_checked"
STATUS_ORDER = {FAIL: 0, WARN: 1, INFO: 2, PASS: 3, NOT_CHECKED: 4}

# Severity ranks failures for inputs.json: 3 = blocks every answer engine, 2 = loses citations or trust, 1 = polish.
CHECKS = {
    "ACC-01": (3, "robots.txt is reachable and served as plain text"),
    "ACC-02": (3, "Search and AI search crawlers may fetch the site in robots.txt"),
    "ACC-03": (2, "User-triggered AI fetchers may fetch the site in robots.txt"),
    "ACC-04": (2, "Training crawlers may fetch the site in robots.txt"),
    "ACC-05": (3, "AI user agents get the same page as a browser"),
    "ACC-06": (2, "A sitemap is declared and readable"),
    "ACC-07": (1, "Sitemap lastmod dates are present and believable"),
    "ACC-08": (1, "robots.txt states content-use preferences (Content-Signal)"),
    "ACC-09": (2, "Sampled pages answer 200 without redirect chains"),
    "ACC-10": (2, "Missing pages answer 404 or 410"),
    "ACC-11": (1, "Bots that inherit other bots' rules get the rules you meant"),
    "ACC-16": (1, "Crawl-delay is not the only throttle"),
    "ACC-17": (2, "Agents, previews, ads, and every other bot type may fetch the site in robots.txt"),
    "ACC-18": (2, "Every other known bot gets the same page as a browser"),
    "REN-01": (3, "The home page's main text is in the raw HTML"),
    "REN-02": (2, "Every sampled page has an h1 in the raw HTML"),
    "REN-03": (3, "No sampled page is an empty JavaScript shell"),
    "REN-04": (2, "Prices are in the raw HTML of the pricing page"),
    "REN-05": (2, "HTML stays under Googlebot's 2 MB limit"),
    "REN-06": (2, "Navigation is plain <a href> links in the raw HTML"),
    "IDX-01": (3, "No sampled page is noindex"),
    "IDX-02": (2, "No sampled page forbids snippets"),
    "IDX-03": (2, "Canonical URLs are present, absolute, and on this site"),
    "IDX-04": (1, "The sitemap lists only indexable, working pages"),
    "IDX-05": (2, "No page carries noarchive (it removes the page from Copilot answers)"),
    "LLM-01": (1, "/llms.txt exists and is plain text"),
    "LLM-02": (1, "llms.txt starts with an H1 naming the site"),
    "LLM-03": (2, "llms.txt opens with a blockquote summary"),
    "LLM-04": (1, "llms.txt has H2 sections of Markdown links"),
    "LLM-05": (2, "llms.txt links resolve"),
    "LLM-06": (2, "llms.txt holds no internal leftovers or instructions to models"),
    "LLM-07": (2, "llms.txt claims match what the server does"),
    "LLM-08": (1, "/llms-full.txt, if published, is a usable size"),
    "LLM-09": (1, "llms.txt is small and fast enough for one fetch"),
    "LLM-10": (1, "llms.txt is linked from the pages agents already fetch"),
    "LLM-11": (1, "The llms.txt Optional section holds only secondary links"),
    "MD-01": (2, "Pages have Markdown mirrors"),
    "MD-02": (1, "Accept: text/markdown returns Markdown, with Vary: Accept"),
    "MD-03": (2, "Markdown mirrors declare their canonical HTML page or are noindex"),
    "MD-04": (1, "HTML pages advertise their Markdown mirror"),
    "MD-05": (1, "Markdown mirrors are clean Markdown"),
    "MD-06": (2, "Markdown responses send the same content signals as robots.txt"),
    "SCH-01": (2, "Every JSON-LD block parses"),
    "SCH-02": (2, "The home page declares an Organization (or Person) and WebSite"),
    "SCH-03": (3, "The Organization name matches the brand"),
    "SCH-04": (2, "Every page names the same Organization"),
    "SCH-05": (3, "Structured data holds no template or placeholder leftovers"),
    "SCH-06": (2, "Structured-data dates are real"),
    "SCH-07": (2, "FAQPage questions are visible on the page"),
    "SCH-08": (2, "Schema URLs point at this site"),
    "SCH-09": (3, "Marked-up prices equal the visible prices"),
    "SCH-10": (1, "No reliance on retired rich-result types"),
    "SCH-11": (1, "Article authors are real, named people"),
    "SCH-12": (1, "The Organization node is complete"),
    "ENT-01": (1, "The Organization lists sameAs profiles"),
    "MSG-01": (2, "The brand name is the same everywhere"),
    "MSG-02": (3, "Prices agree across pages and llms.txt"),
    "MSG-03": (2, "Free and paid claims agree"),
    "MSG-04": (2, "Launch-status words agree"),
    "CNT-01": (1, "Every sampled page has a title and meta description"),
    "CNT-02": (1, "Titles are unique"),
    "CNT-03": (1, "Few sampled pages are thin"),
    "CNT-04": (1, "Headings are phrased as the questions people ask"),
    "CNT-05": (1, "Articles show a visible date"),
    "CNT-06": (1, "Comparison or alternatives pages exist"),
    "DSC-01": (1, "security.txt is valid"),
    "DSC-02": (1, "An MCP server, if any, publishes a server card"),
    "DSC-03": (1, "An RSS or Atom feed is linked"),
    "DSC-04": (1, "An agent resource manifest uses the current ard.json path"),
    "DSC-05": (1, "An API catalog points to the OpenAPI description"),
    "DSC-06": (1, "An agent skills index is published"),
    "DSC-07": (1, "The home page Link header advertises agent resources"),
    "MEA-04": (1, "The site is verified in Bing Webmaster Tools and Search Console"),
}

SEARCH_MUST = ("OAI-SearchBot", "Googlebot", "bingbot", "PerplexityBot", "Claude-SearchBot")
FAIL_CATEGORIES = {"search", "user", "agent"}  # Blocking these removes the site from answers or from agents acting for a person.
OTHER_CATEGORIES = ("agent", "preview", "ads", "other", "infra", "catch-all")
PLACEHOLDER_RE = re.compile(r"\b(lorem ipsum|example\.(com|org)|your (company|business|brand|name)|company name|yourdomain|acme( corp| inc)?|john doe|jane doe|xxx+|placeholder)\b", re.I)
LOCAL_PATH_RE = re.compile(r"([A-Za-z]:\\\\?Users\\|/Users/[a-z]|/home/[a-z]|\b(src|app|lib|next-app|backend)/[\w./-]+\.(js|jsx|ts|tsx|py)\b)")
QUESTION_RE = re.compile(r"^(how|what|why|when|which|who|where|can|does|do|is|are|should|will)\b|\?$", re.I)
TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—·:]\s+")
CONTENT_SIGNAL_RE = re.compile(r"^\s*(search|ai-input|ai-train)\s*=\s*(yes|no)(\s*,\s*(search|ai-input|ai-train)\s*=\s*(yes|no))*\s*$", re.I)
RETIRED_TYPES = {"FAQPage": "FAQ rich results ended 2026-05-07", "HowTo": "retired 2023", "ClaimReview": "retired 2025-06-12", "SpecialAnnouncement": "retired 2025-06-12",
                 "EstimatedSalary": "retired 2025-06-12", "VehicleListing": "retired 2025-06-12", "LearningResource": "Learning Video retired 2025-06-12"}
GENERIC_AUTHORS = {"admin", "administrator", "team", "staff", "editor", "editorial team", "marketing team", "author", "webmaster", "guest"}
ARTICLE_RE = re.compile(r"/(blog|posts?|articles?|news|guides?|stories|insights)/.+")
DATE_TEXT_RE = re.compile(r"\b(20\d\d-\d\d-\d\d|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(st|nd|rd|th)?,? 20\d\d|\d{1,2}(st|nd|rd|th)? (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* 20\d\d)\b", re.I)


class Amounts:
    """Normalizes price strings so "$49", "$49.00", and "$49/mo" compare as one amount."""

    @staticmethod
    def normalize(price: str) -> str:
        # The numeric amount with trailing zeros dropped ("$1,000.00" -> "1000", "$0" -> "0").
        digits = re.sub(r"[^\d.]", "", price.split("/")[0].split(" per ")[0]).strip(".")
        try:
            return ("%.2f" % float(digits)).rstrip("0").rstrip(".")
        except ValueError:
            return digits


class CheckResult:
    """One check's outcome with its evidence tags."""

    def __init__(self, check_id: str, status: str, detail: str, evidence: list = None) -> None:
        # Severity and title come from CHECKS so ids, titles, and severities never drift apart.
        self.id = check_id
        self.severity, self.title = CHECKS[check_id]
        self.family = check_id.split("-")[0]
        self.status = status
        self.detail = detail
        self.evidence = list(evidence or [])

    def to_json(self) -> dict:
        # The result as written to checks.json.
        return {"id": self.id, "family": self.family, "title": self.title, "severity": self.severity, "status": self.status, "detail": self.detail, "evidence": self.evidence}


class BrandResolver:
    """Works out the brand name a site uses from its titles, og:site_name, llms.txt H1, and host."""

    @staticmethod
    def normalize(text: str) -> str:
        # Lowercase letters and digits only, so "Acme, Inc." and "acme inc" compare equal.
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    @staticmethod
    def title_suffixes(pages: list) -> list:
        # The last segment of each multi-part title ("Pricing | Acme" -> "Acme").
        suffixes = []
        for page in pages:
            parts = TITLE_SPLIT_RE.split(page.get("title") or "")
            if len(parts) >= 2 and 1 <= len(parts[-1]) <= 40:
                suffixes.append(parts[-1].strip())
        return suffixes

    @classmethod
    def sources(cls, facts: dict) -> dict:
        # {source label: [brand strings]} from every place a brand name appears.
        pages = facts.get("pages", [])
        llms_h1 = LlmsTxt(facts.get("llms", {}).get("text", "")).h1
        org_names = [n for p in pages for n in OrgNodes.names(p)]
        return {
            "title suffix": cls.title_suffixes(pages),
            "og:site_name": [p["og_site_name"] for p in pages if p.get("og_site_name")],
            "llms.txt H1": [llms_h1] if llms_h1 else [],
            "Organization schema": org_names,
        }

    @classmethod
    def brand(cls, facts: dict):
        # The most common brand string outside schema, or None when the site states none.
        sources = cls.sources(facts)
        votes = Counter()
        for label in ("title suffix", "og:site_name", "llms.txt H1"):
            for name in sources[label]:
                votes[name.strip()] += 1
        return votes.most_common(1)[0][0] if votes else None

    @classmethod
    def matches(cls, name: str, brand: str, host: str) -> bool:
        # True when name is the brand or the host's main label, allowing suffixes like "Inc" or "App".
        n = cls.normalize(name)
        if not n:
            return False
        label = cls.normalize((host or "").split(".")[-2] if host and host.count(".") >= 1 else host)
        for other in (cls.normalize(brand), label):
            if other and (n == other or (len(min(n, other, key=len)) >= 4 and (n in other or other in n))):
                return True
        return False


class OrgNodes:
    """Finds Organization-like nodes in a page's JSON-LD."""

    ORG_TYPES = {"Organization", "Corporation", "LocalBusiness", "OnlineBusiness", "OnlineStore", "NGO", "EducationalOrganization", "ProfessionalService"}

    @classmethod
    def of(cls, page: dict) -> list:
        # Every top-level node whose @type is an Organization type; Person nodes (authors) are not the site's identity.
        return [n for n in page.get("jsonld", []) if cls.ORG_TYPES.intersection(JsonLd.types(n))]

    @classmethod
    def names(cls, page: dict) -> list:
        # The string names of a page's Organization nodes.
        return [str(n.get("name")).strip() for n in cls.of(page) if isinstance(n.get("name"), str) and n.get("name").strip()]


class LlmsTxt:
    """Reads the structure the llms.txt proposal defines."""

    def __init__(self, text: str) -> None:
        # Keeps the non-empty lines for structure checks.
        self.text = text or ""
        self.lines = [item.rstrip() for item in self.text.splitlines() if item.strip()]

    @property
    def h1(self):
        # The text of the first line when it is "# Title", else None.
        return self.lines[0][2:].strip() if self.lines and re.match(r"#\s+\S", self.lines[0]) else None

    @property
    def blockquote(self) -> str:
        # The first blockquote within the first 8 non-empty lines, joined.
        quote = []
        for line in self.lines[1:9]:
            if line.lstrip().startswith(">"):
                quote.append(line.lstrip()[1:].strip())
            elif quote:
                break
        return " ".join(quote)

    @property
    def h2_count(self) -> int:
        # Number of "## " section headings.
        return sum(1 for item in self.lines if re.match(r"##\s+\S", item))

    @property
    def link_count(self) -> int:
        # Number of Markdown links.
        return len(re.findall(r"\[[^\]]*\]\([^)]+\)", self.text))


class CheckSuite:
    """Runs every mechanical check over one site's facts."""

    def __init__(self, facts: dict, robots: RobotsTxt, evidence: EvidenceIndex, today: dt.date = None) -> None:
        # today is injectable so the date checks are testable.
        self.facts = facts
        self.robots = robots
        self.ev = evidence
        self.today = today or dt.date.today()
        self.results: list = []
        self.origin = facts.get("origin", "")
        self.pages = [p for p in facts.get("pages", []) if p.get("status") and 200 <= p["status"] < 300]

    def run(self) -> list:
        # Every family in order; returns the CheckResult list.
        for family in (self.access, self.reachability, self.rendering, self.indexing, self.llms_txt, self.markdown, self.schema, self.schema_facts, self.messages, self.content, self.discovery):
            family()
        return self.results

    @staticmethod
    def not_audited(reason: str) -> list:
        # Every check as not_checked, for question-only runs.
        return [CheckResult(cid, NOT_CHECKED, reason) for cid in CHECKS]

    def add(self, check_id: str, status: str, detail: str, evidence: list = None) -> None:
        # Records one result.
        self.results.append(CheckResult(check_id, status, detail, evidence))

    def access(self) -> None:
        # ACC-01..ACC-09, ACC-17, ACC-18: robots.txt, every bot token, user-agent parity, sitemaps, page status.
        r = self.facts.get("robots", {})
        url = self.origin + "/robots.txt"
        if self.robots.unreachable:
            tag = self.ev.add("robots", url, "GET /robots.txt -> %s %s. Crawlers treat an unreachable robots.txt as disallow-all." % (r.get("status"), r.get("error") or ""))
            self.add("ACC-01", FAIL, "robots.txt is unreachable (status %s), which most crawlers read as 'disallow everything'." % r.get("status"), [tag])
        elif r.get("served_as_html"):
            tag = self.ev.add("robots", url, "GET /robots.txt -> %s with an HTML body (%s)." % (r.get("status"), r.get("content_type")))
            self.add("ACC-01", FAIL, "robots.txt answers with an HTML page, so it holds no rules and no Sitemap line.", [tag])
        elif r.get("status") in (401, 403):
            tag = self.ev.add("robots", url, "GET /robots.txt -> %s. Crawlers read a 4xx robots.txt as 'no restrictions', then hit the same block on content." % r.get("status"))
            self.add("ACC-01", FAIL, "robots.txt answers %s, which usually means a firewall rule blocks the request; crawlers treat it as allow-all and then get blocked on pages." % r.get("status"), [tag])
        elif r.get("status") == 404:
            tag = self.ev.add("robots", url, "GET /robots.txt -> 404 (everything allowed, no Sitemap line).")
            self.add("ACC-01", WARN, "No robots.txt: everything is allowed, but there is no place to declare the sitemap or AI preferences.", [tag])
        elif r.get("bytes", 0) > 512_000 or r.get("truncated"):
            tag = self.ev.add("robots", url, "robots.txt is %d bytes; crawlers ignore everything after 500 KiB." % r.get("bytes", 0))
            self.add("ACC-01", WARN, "robots.txt is over 500 KiB, so rules near the end are ignored.", [tag])
        else:
            tag = self.ev.add("robots", url, "GET /robots.txt -> %s, %d rule lines%s." % (r.get("status"), r.get("lines", 0), (", unknown lines: " + "; ".join(r["unknown_lines"][:3])) if r.get("unknown_lines") else ""))
            self.add("ACC-01", WARN if r.get("unknown_lines") else PASS, "robots.txt is reachable" + (" but has lines crawlers ignore." if r.get("unknown_lines") else "."), [tag])
        self._robots_tokens(url)
        self._ua_parity()
        self._sitemap_checks()
        self._page_status()

    def _robots_tokens(self, url: str) -> None:
        # ACC-02..ACC-04 and ACC-17 from the robots verdict for every registry token on "/" and on the sampled paths.
        paths = ["/"] + [urllib.parse.urlsplit(p["url"]).path or "/" for p in self.pages[:10]]
        verdicts = {}
        for row in self.facts.get("robots", {}).get("tokens", []):
            # A bot that obeys only a group naming it (AdsBot-Google and friends) is unaffected by * rules.
            unaffected = row.get("named_only") and not row["named"]
            blocked = [] if unaffected else [p for p in paths if not self.robots.allowed(row["agent"], p)]
            verdicts[row["agent"]] = (row["purpose"], row["named"], blocked)
        search_blocked = self._blocked(verdicts, ("search",))
        if search_blocked:
            tag = self.ev.add("robots", url, "Blocked for search crawlers: " + "; ".join("%s on %s" % (a, ", ".join(b[:3])) for a, b in search_blocked))
            must = [a for a, _ in search_blocked if a in SEARCH_MUST]
            self.add("ACC-02", FAIL if must else WARN, "robots.txt blocks search crawlers: %s." % ", ".join(a for a, _ in search_blocked), [tag])
        else:
            self.add("ACC-02", PASS, "Every search and AI search crawler in the bot list may fetch the home page and the sampled pages.", [])
        user_blocked = self._blocked(verdicts, ("user",))
        if user_blocked:
            tag = self.ev.add("robots", url, "Blocked for user-triggered fetchers: " + ", ".join(a for a, _ in user_blocked))
            self.add("ACC-03", WARN, "robots.txt blocks fetchers that act for a person asking about this site: %s." % ", ".join(a for a, _ in user_blocked), [tag])
        else:
            self.add("ACC-03", PASS, "User-triggered fetchers are allowed.", [])
        training_blocked = self._blocked(verdicts, ("training",))
        if training_blocked:
            tag = self.ev.add("robots", url, "Blocked for training crawlers: " + ", ".join(a for a, _ in training_blocked))
            self.add("ACC-04", WARN, "robots.txt blocks training crawlers: %s. This does not remove the site from AI search answers, but the models learn less about it." % ", ".join(a for a, _ in training_blocked), [tag])
        else:
            self.add("ACC-04", PASS, "Training crawlers are allowed.", [])
        other_blocked = self._blocked(verdicts, OTHER_CATEGORIES)
        if other_blocked:
            labels = ["%s (%s)" % (a, verdicts[a][0]) for a, _ in other_blocked]
            tag = self.ev.add("robots", url, "Blocked for other bots: " + ", ".join(labels))
            severe = any(verdicts[a][0] in FAIL_CATEGORIES for a, _ in other_blocked)
            self.add("ACC-17", FAIL if severe else WARN, "robots.txt blocks other bot types: %s." % ", ".join(labels), [tag])
        else:
            self.add("ACC-17", PASS, "Agents, link previews, ads checks, and generic clients are allowed by robots.txt.", [])

    @staticmethod
    def _blocked(verdicts: dict, purposes) -> list:
        # [(token, blocked paths)] for the tokens in these categories that robots.txt blocks somewhere.
        return [(a, v[2]) for a, v in verdicts.items() if v[0] in purposes and v[2]]

    def _ua_parity(self) -> None:
        # ACC-05 for the core AI and search crawlers, ACC-18 for every other bot: each gets the browser's status and about its text.
        probes = self.facts.get("ua_probes", [])
        if not probes:
            self.add("ACC-05", NOT_CHECKED, "No user-agent probes ran.")
            self.add("ACC-18", NOT_CHECKED, "No user-agent probes ran.")
            return
        base = probes[0]
        core = [row for row in probes[1:] if row["agent"] in CORE_BOTS]
        others = [row for row in probes[1:] if row["agent"] not in CORE_BOTS]
        # The core crawlers feed ChatGPT, Claude, Perplexity, Google, Bing, and Apple answers, so any refusal fails.
        bad, tags = self._parity_failures(base, core)
        if bad:
            self.add("ACC-05", FAIL, "The home page answers AI and search crawlers differently from a browser: %s. A firewall or bot rule is likely blocking them." % ", ".join(r["agent"] for r in bad), tags)
        else:
            tag = self.ev.add("ua", self.origin + "/", "All %d core AI and search user agents got status %s like the browser (%d words). Sent from this machine, so IP-verified bot rules were not exercised." % (len(core), base["status"], base["words"]))
            self.add("ACC-05", PASS, "Every core AI and search crawler got the browser's page.", [tag])
        # Every other bot: refusing an agent, search, or user fetcher fails; previews, ads, and generic clients warn.
        bad, tags = self._parity_failures(base, others)
        behind_cloudflare = "cloudflare" in str((self.facts.get("home") or {}).get("server") or "").lower()
        # Infra bots (Cloudflare's own monitors) only ever visit sites on that CDN.
        counted = [r for r in bad if r.get("purpose") != "infra" or behind_cloudflare]
        if counted:
            severe = any(r.get("purpose") in FAIL_CATEGORIES for r in counted)
            self.add("ACC-18", FAIL if severe else WARN, "The firewall or server treats these bots differently from a browser: %s." % ", ".join("%s (%s)" % (r["agent"], r.get("purpose")) for r in counted), tags)
        elif bad:
            self.add("ACC-18", INFO, "Only CDN infrastructure bots were refused, and the site does not look like it is behind that CDN: %s." % ", ".join(r["agent"] for r in bad), tags)
        else:
            tag = self.ev.add("ua", self.origin + "/", "All %d other bot user agents (previews, ads, agents, generic clients) got status %s like the browser." % (len(others), base["status"]))
            self.add("ACC-18", PASS, "Every other known bot got the browser's page.", [tag])

    def _parity_failures(self, base: dict, rows: list):
        # The rows whose status, text, or challenge differs from the browser's, with one evidence tag each.
        bad, tags = [], []
        for row in rows:
            dropped = base["words"] and row["words"] < 0.5 * base["words"]
            challenged = row.get("challenge") and not base.get("challenge")
            if row["status"] != base["status"] or dropped or challenged:
                bad.append(row)
                tags.append(self.ev.add("ua", self.origin + "/", "GET / as %s -> %s, %d words%s (browser: %s, %d words)%s." % (row["agent"], row["status"], row["words"], ", challenge page" if challenged else "", base["status"], base["words"], (" " + row["error"]) if row.get("error") else "")))
        return bad, tags

    def _sitemap_checks(self) -> None:
        # ACC-06 sitemap declared and readable; ACC-07 lastmod present and believable.
        files = self.facts.get("sitemaps", [])
        declared = bool(self.facts.get("robots", {}).get("sitemaps"))
        readable = [f for f in files if f.get("status") and 200 <= f["status"] < 300 and (f.get("urls") or f.get("index"))]
        tag = self.ev.add("sitemap", self.origin, "Sitemaps: " + "; ".join("%s -> %s (%s URLs)" % (f["url"], f.get("status"), f.get("urls")) for f in files[:5]) + ("; declared in robots.txt" if declared else "; not declared in robots.txt"))
        if not readable:
            self.add("ACC-06", FAIL, "No readable sitemap was found.", [tag])
        else:
            self.add("ACC-06", PASS if declared else WARN, "A sitemap is readable" + ("" if declared else " but robots.txt does not declare it."), [tag])
        lastmods = list(self.facts.get("sitemap_lastmod", {}).values())
        urls = self.facts.get("sitemap_urls", [])
        if not urls:
            self.add("ACC-07", NOT_CHECKED, "No sitemap URLs to read.")
            return
        days = Counter(lm[:10] for lm in lastmods)
        if not lastmods:
            self.add("ACC-07", WARN, "No sitemap entry has a lastmod date.", [tag])
        elif len(lastmods) >= 5 and days.most_common(1)[0][1] >= 0.9 * len(lastmods):
            day, count = days.most_common(1)[0]
            tag2 = self.ev.add("sitemap", self.origin, "%d of %d lastmod dates are %s." % (count, len(lastmods), day))
            self.add("ACC-07", WARN, "Almost every lastmod is the same day (%s), so engines learn to ignore them." % day, [tag2])
        else:
            self.add("ACC-07", PASS, "%d of %d sitemap URLs carry varied lastmod dates." % (len(lastmods), len(urls)), [tag])
        signals = self.facts.get("robots", {}).get("content_signals", [])
        if signals:
            tag = self.ev.add("robots", self.origin + "/robots.txt", "Content-Signal: " + "; ".join(signals[:3]))
            invalid = [s for s in signals if not CONTENT_SIGNAL_RE.match(s)]
            no_input = [s for s in signals if re.search(r"ai-input\s*=\s*no", s, re.I)]
            if invalid:
                self.add("ACC-08", WARN, "Content-Signal lines do not follow the search/ai-input/ai-train=yes|no grammar: %s" % "; ".join(invalid[:2]), [tag])
            elif no_input:
                self.add("ACC-08", WARN, "Content-Signal says ai-input=no, which asks AI answers not to use the content; check that this is intended.", [tag])
            else:
                self.add("ACC-08", INFO, "Content-Signal: " + "; ".join(signals[:3]), [tag])
        else:
            self.add("ACC-08", INFO, "robots.txt has no Content-Signal line (optional; preferences only).", [])

    def _page_status(self) -> None:
        # ACC-09: sampled pages answer 2xx, and none needs more than one redirect.
        sampled = self.facts.get("pages", [])
        broken = [p for p in sampled if not p.get("status") or p["status"] >= 400]
        chains = [p for p in sampled if p.get("redirects", 0) > 1]
        tags = [self.ev.add("page", p["url"], "GET -> %s%s" % (p.get("status"), (" " + p["error"]) if p.get("error") else "")) for p in broken[:5]]
        tags += [self.ev.add("page", p["url"], "%d redirects before the final page %s" % (p["redirects"], p.get("final_url"))) for p in chains[:3]]
        if broken:
            self.add("ACC-09", FAIL, "%d of %d sampled pages failed." % (len(broken), len(sampled)), tags)
        elif chains:
            self.add("ACC-09", WARN, "%d sampled pages sit behind redirect chains." % len(chains), tags)
        else:
            self.add("ACC-09", PASS, "All %d sampled pages answered 2xx." % len(sampled), [])

    def rendering(self) -> None:
        # REN-01..REN-04: what a crawler that runs no JavaScript sees.
        home = self.facts.get("home") or {}
        if not home.get("status") or home["status"] >= 400:
            for cid in ("REN-01", "REN-02", "REN-03", "REN-04"):
                self.add(cid, NOT_CHECKED, "The home page did not load.")
            return
        tag = self.ev.add("page", home["url"], "Raw HTML: %d visible words, h1=%s, frameworks=%s. First words: %s" % (home["words"], home["h1"][:1] or "none", ",".join(home["frameworks"]) or "none", home.get("text_sample", "")[:160]))
        self.add("REN-01", PASS if home["words"] >= 150 else FAIL, "The home page has %d words of visible text in its raw HTML%s." % (home["words"], "" if home["words"] >= 150 else "; crawlers that do not run JavaScript see almost nothing"), [tag])
        no_h1 = [p for p in self.pages if not p.get("h1")]
        tags = [self.ev.add("page", p["url"], "No h1 in raw HTML (title: %s)." % (p.get("title") or "none")) for p in no_h1[:5]]
        self.add("REN-02", PASS if not no_h1 else WARN, "%d of %d sampled pages lack an h1 in raw HTML." % (len(no_h1), len(self.pages)), tags)
        shells = [p for p in self.pages if p["words"] < 60 and p.get("frameworks")]
        tags = [self.ev.add("page", p["url"], "Raw HTML has %d words; framework markers: %s." % (p["words"], ", ".join(p["frameworks"]))) for p in shells[:5]]
        self.add("REN-03", FAIL if shells else PASS, ("%d sampled pages are near-empty JavaScript shells." % len(shells)) if shells else "No sampled page is an empty JavaScript shell.", tags)
        pricing = [p for p in self.pages if re.search(r"/(pricing|prices|plans)\b", urllib.parse.urlsplit(p["url"]).path)]
        if not pricing:
            self.add("REN-04", NOT_CHECKED, "No pricing page was sampled.")
        else:
            missing = [p for p in pricing if not p["claims"]["prices"]]
            tags = [self.ev.add("page", p["url"], "Pricing page raw HTML has no price string; %d words." % p["words"]) for p in missing]
            self.add("REN-04", WARN if missing else PASS, "Prices are missing from the raw HTML of the pricing page." if missing else "Pricing pages show prices in raw HTML.", tags)

    def indexing(self) -> None:
        # IDX-01..IDX-04: noindex, snippet limits, canonicals, sitemap hygiene.
        listed = set(self.facts.get("sitemap_lastmod", {})) | {loc for loc, _ in self.facts.get("sitemap_urls", [])}
        noindex, nosnippet, tags, snip_tags = [], [], [], []
        for p in self.pages:
            directives = " ".join(filter(None, [p.get("robots_meta"), p.get("x_robots_tag")])).lower()
            if "noindex" in directives or "none" in directives.split(","):
                noindex.append(p)
                tags.append(self.ev.add("page", p["url"], "robots directives: %s%s" % (directives, " (listed in the sitemap)" if p["url"] in listed else "")))
            if "nosnippet" in directives or re.search(r"max-snippet\s*:\s*0\b", directives):
                nosnippet.append(p)
                snip_tags.append(self.ev.add("page", p["url"], "snippet directives: %s" % directives))
        self.add("IDX-01", FAIL if noindex else PASS, ("%d sampled pages are noindex." % len(noindex)) if noindex else "No sampled page is noindex.", tags[:5])
        self.add("IDX-02", WARN if nosnippet else PASS, ("%d sampled pages forbid snippets, which also keeps them out of AI Overviews and AI Mode." % len(nosnippet)) if nosnippet else "No sampled page forbids snippets.", snip_tags[:5])
        bad, ctags = [], []
        host = urllib.parse.urlsplit(self.origin).netloc
        for p in self.pages:
            canonical = p.get("canonical")
            problem = None
            if not canonical:
                problem = "no canonical link"
            elif not canonical.startswith(("http://", "https://")):
                problem = "relative canonical %s" % canonical
            elif urllib.parse.urlsplit(canonical).netloc.lower() not in (host, "www." + host, host.replace("www.", "", 1)):
                problem = "canonical points to another host: %s" % canonical
            if problem:
                bad.append(p)
                ctags.append(self.ev.add("page", p["url"], problem))
        self.add("IDX-03", (FAIL if any("another host" in self.ev.items[t]["text"] for t in ctags) else WARN) if bad else PASS, ("%d of %d sampled pages have canonical problems." % (len(bad), len(self.pages))) if bad else "Canonicals are present and absolute.", ctags[:5])
        listed_bad = [p for p in self.facts.get("pages", []) if p["url"] in listed and (not p.get("status") or p["status"] >= 300 or p in noindex)]
        tags = [self.ev.add("sitemap", p["url"], "Listed in the sitemap but answers %s%s" % (p.get("status"), " and is noindex" if p in noindex else "")) for p in listed_bad[:5]]
        self.add("IDX-04", WARN if listed_bad else (PASS if listed else NOT_CHECKED), ("%d sitemap URLs are broken, redirected, or noindex." % len(listed_bad)) if listed_bad else ("Sampled sitemap URLs are indexable." if listed else "No sitemap to compare."), tags)

    def llms_txt(self) -> None:
        # LLM-01..LLM-08: the llms.txt proposal's structure, hygiene, and honesty.
        llms = self.facts.get("llms", {})
        url = llms.get("url", self.origin + "/llms.txt")
        if not llms.get("is_text"):
            tag = self.ev.add("llms", url, "GET /llms.txt -> %s %s%s" % (llms.get("status"), llms.get("content_type") or "", " (HTML page, not a text file)" if llms.get("status") == 200 else ""))
            self.add("LLM-01", INFO, "There is no /llms.txt. Google Search ignores it and it does not raise AI citations; it helps coding agents and developer tools that are pointed at it.", [tag])
            for cid in ("LLM-02", "LLM-03", "LLM-04", "LLM-05", "LLM-06", "LLM-07", "LLM-09", "LLM-10", "LLM-11"):
                self.add(cid, NOT_CHECKED, "No llms.txt.")
        else:
            self._llms_structure(llms, url)
        full = self.facts.get("llms_full", {})
        self.add("LLM-08", PASS if full.get("is_text") else INFO, ("/llms-full.txt exists (%d bytes)." % full.get("bytes", 0)) if full.get("is_text") else "No /llms-full.txt (optional).", [])

    def _llms_structure(self, llms: dict, url: str) -> None:
        # Runs LLM-01..LLM-07 on an llms.txt that exists.
        doc = LlmsTxt(llms.get("text", ""))
        tag = self.ev.add("llms", url, "GET /llms.txt -> %s %s, %d bytes, %d lines, %d H2 sections, %d links. First line: %s" % (llms["status"], llms.get("content_type"), llms.get("bytes", 0), len(doc.lines), doc.h2_count, doc.link_count, doc.lines[0][:120] if doc.lines else ""))
        self.add("LLM-01", PASS, "/llms.txt exists.", [tag])
        self.add("LLM-02", PASS if doc.h1 else FAIL, ("H1: %s" % doc.h1) if doc.h1 else "The first line is not a '# Title' H1, which the format requires.", [tag])
        quote = doc.blockquote
        qtag = self.ev.add("llms", url, "Blockquote: " + (quote[:250] or "none in the first 8 lines"))
        self.add("LLM-03", PASS if len(quote) >= 40 else FAIL, "llms.txt opens with a summary blockquote." if len(quote) >= 40 else "llms.txt has no summary blockquote after the H1, so an agent must read prose to learn what the site is.", [qtag])
        self.add("LLM-04", PASS if doc.h2_count and doc.link_count >= 3 else WARN, "%d H2 sections and %d links." % (doc.h2_count, doc.link_count), [tag])
        links = self.facts.get("llms_links", [])
        broken = [item for item in links if not item.get("status") or item["status"] >= 400]
        ltags = [self.ev.add("llms", item["url"], "Linked from llms.txt; GET -> %s" % item.get("status")) for item in broken[:5]]
        if not links:
            self.add("LLM-05", NOT_CHECKED, "llms.txt has no same-site links to check.")
        else:
            self.add("LLM-05", FAIL if broken else PASS, ("%d of %d sampled llms.txt links are broken." % (len(broken), len(links))) if broken else "All %d sampled llms.txt links resolve (%d serve Markdown)." % (len(links), sum(1 for item in links if item.get("markdown"))), ltags)
        problems = []
        text = llms.get("text", "")
        if "<!--" in text:
            problems.append("an HTML comment: " + text[text.index("<!--"):text.index("<!--") + 120].replace("\n", " "))
        local = LOCAL_PATH_RE.search(text)
        if local:
            problems.append("an internal path: " + local.group(0))
        if re.search(r"\bTODO\b|\bFIXME\b|lorem ipsum", text, re.I):
            problems.append("a TODO/FIXME/lorem leftover")
        if llms.get("injection"):
            problems.append("instruction-like text aimed at models: " + "; ".join(llms["injection"]))
        ptags = [self.ev.add("llms", url, "llms.txt contains " + p) for p in problems]
        self.add("LLM-06", FAIL if problems else PASS, ("llms.txt contains " + "; ".join(p.split(":")[0] for p in problems) + ".") if problems else "No internal leftovers or instructions to models.", ptags)
        self._llms_claims(text, url)
        self._llms_delivery(llms, doc, url)

    def _llms_delivery(self, llms: dict, doc: "LlmsTxt", url: str) -> None:
        # LLM-09 size and speed, LLM-10 discoverability, LLM-11 the Optional section.
        size, ms = llms.get("bytes", 0), llms.get("elapsed_ms", 0)
        tag = self.ev.add("llms", url, "llms.txt: %d bytes, fetched in %d ms." % (size, ms))
        slow, big = ms > 2000, size > 100_000 or llms.get("truncated")
        self.add("LLM-09", WARN if slow or big else PASS, ("llms.txt is %s; split it into linked sub-indexes or cache it at the edge." % " and ".join(filter(None, ["over 100,000 bytes" if big else "", "slower than 2 s (Lighthouse's timeout)" if slow else ""]))) if slow or big else "Small and fast.", [tag])
        home = self.facts.get("home") or {}
        mirrors = [p["md_mirror"] for p in self.facts.get("pages", []) if p.get("md_mirror")]
        pointers = []
        if home.get("mentions_llms_txt"):
            pointers.append("home page link or Link header")
        if any("llms" in (m.get("first_line") or "").lower() for m in mirrors):
            pointers.append("Markdown mirrors")
        self.add("LLM-10", PASS if pointers else INFO, ("llms.txt is pointed to from: " + ", ".join(pointers)) if pointers else "Nothing the agent already fetched points to llms.txt (no home-page link, Link header, or mirror pointer); agents fetch it only when directed.", [])
        lines = doc.text.splitlines()
        start = next((i for i, item in enumerate(lines) if re.match(r"##\s+Optional\s*$", item.strip(), re.I)), None)
        if start is None:
            self.add("LLM-11", PASS, "No Optional section.", [])
            return
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        optional = re.findall(r"\]\(([^)]+)\)", "\n".join(lines[start:end]))
        core = [u for u in optional if re.search(r"/(quickstart|getting-started|api|reference|pricing)\b", u)]
        heavy = doc.link_count and len(optional) > 0.5 * doc.link_count
        otag = self.ev.add("llms", url, "Optional section holds %d of %d links; core-looking: %s" % (len(optional), doc.link_count, ", ".join(core[:3]) or "none"))
        self.add("LLM-11", WARN if heavy or core else PASS, "The Optional section, which tools drop for short contexts, holds core links." if heavy or core else "The Optional section holds secondary links.", [otag])

    def _llms_claims(self, text: str, url: str) -> None:
        # LLM-07: a claim that Markdown versions exist must match the observed mirrors and negotiation.
        claims_md = re.search(r"text/markdown|Accept:\s*text|\.md\b|markdown version", text, re.I)
        if not claims_md:
            self.add("LLM-07", PASS, "llms.txt makes no Markdown-serving claim to verify.", [])
            return
        negotiated = self.facts.get("negotiation", {}).get("markdown")
        mirrors = any(p.get("md_mirror") for p in self.facts.get("pages", []))
        md_links = any(item.get("markdown") for item in self.facts.get("llms_links", []))
        says_accept = re.search(r"text/markdown|Accept:", text, re.I)
        tag = self.ev.add("llms", url, "llms.txt mentions '%s'; Accept: text/markdown returned Markdown: %s; .md mirrors found: %s." % (claims_md.group(0), bool(negotiated), bool(mirrors or md_links)))
        if says_accept and not negotiated:
            self.add("LLM-07", FAIL, "llms.txt says Markdown is served by content negotiation, but Accept: text/markdown returned HTML.", [tag])
        elif not (mirrors or md_links or negotiated):
            self.add("LLM-07", FAIL, "llms.txt promises Markdown versions, but none were found.", [tag])
        else:
            self.add("LLM-07", PASS, "llms.txt's Markdown claims hold.", [tag])

    def markdown(self) -> None:
        # MD-01..MD-04: mirrors, negotiation, canonical headers, advertisement.
        probed = [p for p in self.facts.get("pages", []) if "md_mirror" in p]
        if not probed:
            for cid in ("MD-01", "MD-02", "MD-03", "MD-04", "MD-05", "MD-06"):
                self.add(cid, NOT_CHECKED, "No page loaded to probe for mirrors.")
            return
        with_md = [p for p in probed if p.get("md_mirror")]
        tag = self.ev.add("md", self.origin, "Markdown mirrors found for %d of %d probed pages: %s" % (len(with_md), len(probed), ", ".join(p["md_mirror"]["url"] for p in with_md[:4]) or "none"))
        self.add("MD-01", PASS if len(with_md) == len(probed) else (WARN if with_md else INFO), "%d of %d probed pages have a Markdown mirror." % (len(with_md), len(probed)), [tag])
        neg = self.facts.get("negotiation", {})
        ntag = self.ev.add("md", self.origin + "/", "GET / with Accept: text/markdown -> %s %s, Markdown: %s, Vary: %s" % (neg.get("status"), neg.get("content_type"), neg.get("markdown"), neg.get("vary")))
        if neg.get("markdown") and "accept" not in (neg.get("vary") or "").lower():
            self.add("MD-02", WARN, "Content negotiation works but the response has no 'Vary: Accept', so a cache can serve Markdown to browsers or HTML to agents.", [ntag])
        else:
            self.add("MD-02", PASS if neg.get("markdown") else INFO, "Accept: text/markdown returns Markdown." if neg.get("markdown") else "Accept: text/markdown returns HTML (optional, but some agents ask for it).", [ntag])
        uncanon = [p for p in with_md if not self._declares_canonical(p["md_mirror"])]
        utags = [self.ev.add("md", p["md_mirror"]["url"], "Mirror headers: Link=%s; X-Robots-Tag=%s" % (p["md_mirror"].get("link_header"), p["md_mirror"].get("x_robots_tag"))) for p in uncanon[:4]]
        if not with_md:
            self.add("MD-03", NOT_CHECKED, "No mirrors to check.")
        else:
            self.add("MD-03", FAIL if uncanon else PASS, ("%d mirrors send neither 'Link: <html url>; rel=\"canonical\"' nor 'X-Robots-Tag: noindex', so search engines can index them as duplicates." % len(uncanon)) if uncanon else "Every mirror declares its canonical page or is noindex.", utags)
        unadvertised = [p for p in with_md if not p.get("md_advertised")]
        atags = [self.ev.add("md", p["url"], "HTML has no <link rel=\"alternate\" type=\"text/markdown\"> although %s exists" % p["md_mirror"]["url"]) for p in unadvertised[:4]]
        if not with_md:
            self.add("MD-04", NOT_CHECKED, "No mirrors to advertise.")
        else:
            self.add("MD-04", WARN if unadvertised else PASS, ("%d pages do not advertise their mirror." % len(unadvertised)) if unadvertised else "Pages advertise their mirrors.", atags)

        dirty = [p for p in with_md if (p["md_mirror"].get("jsx_lines") or 0) > 3]
        dtags = [self.ev.add("md", p["md_mirror"]["url"], "%d lines of raw HTML/JSX components in the Markdown" % p["md_mirror"]["jsx_lines"]) for p in dirty[:3]]
        self.add("MD-05", (WARN if dirty else PASS) if with_md else NOT_CHECKED, ("%d mirrors contain raw HTML or framework components." % len(dirty)) if dirty else ("Mirrors are clean Markdown." if with_md else "No mirrors to check."), dtags)
        self._markdown_signals(neg)

    def _markdown_signals(self, neg: dict) -> None:
        # MD-06: a converter that stamps Content-Signal on Markdown must not contradict robots.txt.
        header = neg.get("content_signal")
        robots = " ".join(self.facts.get("robots", {}).get("content_signals", [])).lower()
        if not header:
            self.add("MD-06", NOT_CHECKED if not neg.get("markdown") else PASS, "Markdown responses send no Content-Signal header." if neg.get("markdown") else "No negotiated Markdown response.", [])
            return
        tag = self.ev.add("md", self.origin + "/", "Markdown response Content-Signal: %s%s; robots.txt Content-Signal: %s" % (header, " (Cloudflare Markdown for Agents)" if neg.get("markdown_tokens") else "", robots or "none"))
        clash = re.search(r"ai-train\s*=\s*no", robots) and re.search(r"ai-train\s*=\s*yes", header, re.I)
        self.add("MD-06", FAIL if clash else INFO, "Markdown responses grant ai-train=yes while robots.txt says ai-train=no." if clash else "Markdown responses send Content-Signal: %s." % header, [tag])

    @staticmethod
    def _declares_canonical(mirror: dict) -> bool:
        # True when a mirror's Link header names an absolute, non-.md canonical, or its X-Robots-Tag says noindex/none.
        robots = (mirror.get("x_robots_tag") or "").lower()
        if "noindex" in robots or re.search(r"\bnone\b", robots):
            return True
        for target, params in re.findall(r"<([^>]+)>\s*;([^,]*)", mirror.get("link_header") or ""):
            if re.search(r"rel=\"?canonical", params, re.I):
                return target.startswith(("http://", "https://")) and not target.split("?")[0].endswith(".md")
        return False

    def schema(self) -> None:
        # SCH-01..SCH-08 and ENT-01: parse errors, organization identity, leftovers, dates, FAQ, URLs.
        errors = [(p, e) for p in self.pages for e in p.get("jsonld_errors", [])]
        tags = [self.ev.add("schema", p["url"], "JSON-LD parse error: " + e) for p, e in errors[:5]]
        self.add("SCH-01", FAIL if errors else PASS, ("%d JSON-LD blocks do not parse." % len(errors)) if errors else "All JSON-LD blocks parse.", tags)
        home = self.facts.get("home") or {}
        home_types = sorted({t for n in home.get("jsonld", []) for t in JsonLd.types(n)})
        orgs = OrgNodes.of(home)
        htag = self.ev.add("schema", home.get("url", self.origin), "Home JSON-LD types: %s" % (", ".join(home_types) or "none"))
        orgs = orgs or [n for n in home.get("jsonld", []) if "Person" in JsonLd.types(n)]
        self.add("SCH-02", PASS if orgs and "WebSite" in home_types else (WARN if orgs or home_types else FAIL), "Home page types: %s." % (", ".join(home_types) or "no structured data"), [htag])
        self._org_identity()
        self._leftovers()
        self._dates()
        self._faq()
        self._schema_urls()
        same_as = [n.get("sameAs") for n in orgs if n.get("sameAs")]
        if orgs:
            values = same_as[0] if same_as and isinstance(same_as[0], list) else same_as
            stag = self.ev.add("schema", home.get("url", self.origin), "Organization sameAs: %s" % (", ".join(map(str, values[:6])) if values else "none"))
            self.add("ENT-01", PASS if values else WARN, ("sameAs lists %d profiles." % len(values)) if values else "The Organization has no sameAs links to its profiles (LinkedIn, GitHub, Crunchbase, Wikipedia, Wikidata).", [stag])
        else:
            self.add("ENT-01", NOT_CHECKED, "No Organization node on the home page.")

    def _org_identity(self) -> None:
        # SCH-03 the Organization name matches the brand; SCH-04 one name across pages.
        brand = BrandResolver.brand(self.facts)
        host = self.facts.get("host", "")
        names = Counter(n for p in self.pages for n in OrgNodes.names(p))
        if not names:
            self.add("SCH-03", NOT_CHECKED, "No Organization name in structured data.")
            self.add("SCH-04", NOT_CHECKED, "No Organization name in structured data.")
            return
        wrong = [n for n in names if not BrandResolver.matches(n, brand or "", host)]
        tags = [self.ev.add("schema", next(p["url"] for p in self.pages if n in OrgNodes.names(p)), "Organization name '%s' on %d pages; brand elsewhere: '%s'; host: %s" % (n, names[n], brand or "unknown", host)) for n in wrong[:3]]
        self.add("SCH-03", FAIL if wrong else PASS, ("Structured data names the organization %s, which matches neither the brand '%s' nor the domain." % (", ".join("'%s'" % n for n in wrong), brand or "unknown")) if wrong else "The Organization name matches the brand.", tags)
        ntag = self.ev.add("schema", self.origin, "Organization names across pages: " + ", ".join("%s (%d)" % (n, c) for n, c in names.most_common(5)))
        distinct = {BrandResolver.normalize(n) for n in names}
        self.add("SCH-04", WARN if len(distinct) > 1 else PASS, ("Pages name %d different organizations." % len(distinct)) if len(distinct) > 1 else "One Organization name across pages.", [ntag])

    def _leftovers(self) -> None:
        # SCH-05: template or placeholder values anywhere in JSON-LD strings; a brand that really is "Acme" is not a leftover.
        brand = BrandResolver.normalize(BrandResolver.brand(self.facts) or self.facts.get("host", ""))
        hits = []
        for p in self.pages:
            for value in JsonValues.identity_strings(p.get("jsonld", [])):
                match = PLACEHOLDER_RE.search(value)
                if match and BrandResolver.normalize(match.group(0)) not in brand:
                    hits.append((p, value))
                    break
        tags = [self.ev.add("schema", p["url"], "Placeholder-looking schema value: " + v[:150]) for p, v in hits[:5]]
        self.add("SCH-05", FAIL if hits else PASS, ("%d pages carry placeholder or template values in structured data." % len(hits)) if hits else "No placeholder values found in structured data.", tags)

    def _dates(self) -> None:
        # SCH-06: datePublished/dateModified are not "today" everywhere and modified is not before published.
        today = self.today.isoformat()
        dated, today_pages, backwards = [], [], []
        for p in self.pages:
            for node in p.get("jsonld", []):
                published, modified = str(node.get("datePublished") or "")[:10], str(node.get("dateModified") or "")[:10]
                if not (published or modified):
                    continue
                dated.append(p)
                if today in (published, modified):
                    today_pages.append(p)
                if published and modified and modified < published:
                    backwards.append((p, published, modified))
                break
        tags = [self.ev.add("schema", p["url"], "dateModified or datePublished is today (%s)" % today) for p in today_pages[:3]]
        tags += [self.ev.add("schema", p["url"], "dateModified %s is before datePublished %s" % (m, d)) for p, d, m in backwards[:3]]
        if not dated:
            self.add("SCH-06", NOT_CHECKED, "No structured-data dates on the sampled pages.")
        elif backwards or (len(dated) >= 2 and len(today_pages) >= 0.5 * len(dated)):
            self.add("SCH-06", WARN, "%d of %d dated pages claim today's date, or dates run backwards; a template is probably printing the render time." % (len(today_pages), len(dated)), tags)
        else:
            self.add("SCH-06", PASS, "Structured-data dates look real.", tags)

    def _faq(self) -> None:
        # SCH-07: every FAQPage question appears in the visible text.
        hidden = []
        for p in self.pages:
            text = BrandResolver.normalize(p.get("_fulltext") or p.get("text_sample") or "")
            for node in p.get("jsonld", []):
                if "FAQPage" not in JsonLd.types(node):
                    continue
                entities = node.get("mainEntity") or []
                for q in entities if isinstance(entities, list) else [entities]:
                    name = isinstance(q, dict) and str(q.get("name") or "")
                    if name and BrandResolver.normalize(name)[:40] not in text:
                        hidden.append((p, name))
        tags = [self.ev.add("schema", p["url"], "FAQPage question not in visible text: " + q[:150]) for p, q in hidden[:4]]
        has_faq = any("FAQPage" in JsonLd.types(n) for p in self.pages for n in p.get("jsonld", []))
        if not has_faq:
            self.add("SCH-07", NOT_CHECKED, "No FAQPage markup.")
        else:
            self.add("SCH-07", WARN if hidden else PASS, ("%d FAQ questions in markup do not appear in the raw HTML text (they may render only with JavaScript, or not at all)." % len(hidden)) if hidden else "FAQ markup matches visible questions.", tags)

    def _schema_urls(self) -> None:
        # SCH-08: url/@id/logo values of the site's own nodes point at this host.
        host = urllib.parse.urlsplit(self.origin).netloc.lower().replace("www.", "", 1)
        foreign = []
        for p in self.pages:
            for node in OrgNodes.of(p) + [n for n in p.get("jsonld", []) if "WebSite" in JsonLd.types(n)]:
                for key in ("url", "@id"):
                    value = node.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        other = urllib.parse.urlsplit(value).netloc.lower().replace("www.", "", 1)
                        if other and other != host:
                            foreign.append((p, key, value))
        tags = [self.ev.add("schema", p["url"], "Own node %s points at another host: %s" % (k, v)) for p, k, v in foreign[:4]]
        self.add("SCH-08", WARN if foreign else PASS, ("%d Organization/WebSite URLs point at other hosts (old domain or template)." % len(foreign)) if foreign else "Schema URLs point at this site.", tags)

    def messages(self) -> None:
        # MSG-01..MSG-04: candidate contradictions for the model to judge.
        sources = BrandResolver.sources(self.facts)
        variants = {}
        for label, names in sources.items():
            for name in names:
                variants.setdefault(BrandResolver.normalize(name), set()).add("%s: %s" % (label, name))
        variants.pop("", None)
        tag = self.ev.add("brand", self.origin, "Brand strings: " + " | ".join(sorted({v for vs in variants.values() for v in vs}))[:280])
        if not variants:
            self.add("MSG-01", NOT_CHECKED, "No brand strings found.")
        else:
            self.add("MSG-01", WARN if len(variants) > 1 else PASS, ("%d different brand strings across titles, og:site_name, llms.txt, and schema." % len(variants)) if len(variants) > 1 else "One brand string everywhere.", [tag])
        self._prices()
        self._claims()

    def _prices(self) -> None:
        # MSG-02: prices on the pricing page, other pages, and llms.txt agree.
        by_source = {}
        for p in self.pages:
            if p["claims"]["prices"] and not ARTICLE_RE.search(urllib.parse.urlsplit(p["url"]).path):
                by_source[urllib.parse.urlsplit(p["url"]).path or "/"] = set(p["claims"]["prices"])
        llms_text = self.facts.get("llms", {}).get("text", "")
        from .probe import ClaimFinder  # local import keeps probe -> checks one-directional at import time
        llms_prices = set(ClaimFinder.find(llms_text)["prices"]) if llms_text else set()
        if llms_prices:
            by_source["llms.txt"] = llms_prices
        if len(by_source) < 2:
            self.add("MSG-02", NOT_CHECKED if not by_source else PASS, "Prices appear in %d place(s); nothing to compare." % len(by_source))
            return
        amounts = {k: {Amounts.normalize(v) for v in vals} for k, vals in by_source.items()}
        pricing_keys = [k for k in amounts if re.search(r"pricing|plans|prices", k)]
        reference = set().union(*(amounts[k] for k in pricing_keys)) if pricing_keys else None
        conflicts = []
        if reference:
            for k, vals in amounts.items():
                extra = vals - reference
                if k not in pricing_keys and extra:
                    conflicts.append((k, sorted(extra)))
        tags = [self.ev.add("price", self.origin, "%s shows %s, not on the pricing page (%s)" % (k, ", ".join(v), ", ".join(sorted(reference))[:120])) for k, v in conflicts[:4]]
        tags.append(self.ev.add("price", self.origin, "Prices by place: " + "; ".join("%s: %s" % (k, ", ".join(sorted(v))[:80]) for k, v in by_source.items())[:280]))
        if reference is None:
            self.add("MSG-02", INFO, "Prices appear on several pages but no pricing page was sampled; compare them by reading.", tags)
        else:
            self.add("MSG-02", WARN if conflicts else PASS, ("%d places show amounts the pricing page does not. They may be stale prices or other units; read them." % len(conflicts)) if conflicts else "Amounts elsewhere also appear on the pricing page.", tags)

    def _claims(self) -> None:
        # MSG-03 free vs paid claims and MSG-04 launch-status words, as candidates.
        free = [(p, c) for p in self.pages for c in p["claims"]["free"]]
        paid = any(p["claims"]["prices"] for p in self.pages)
        tags = [self.ev.add("claim", p["url"], "Free claim: " + c) for p, c in free[:4]]
        if not free:
            self.add("MSG-03", PASS if paid else NOT_CHECKED, "No free claims found." if paid else "No free claims or prices found.")
        else:
            self.add("MSG-03", WARN if paid else INFO, ("Pages say '%s' while other pages show prices; check the free tier is described the same way everywhere." % free[0][1][:80]) if paid else "Free claims found; no prices to compare.", tags)
        status_words = Counter()
        found = []
        for p in self.pages:
            for phrase in p["claims"]["status"]:
                word = re.search(r"beta|waitlist|coming soon|early access|invite[- ]only|generally available|now available|open[- ]source", phrase, re.I).group(0).lower()
                status_words[word] += 1
                found.append((p, phrase))
        stages = {w for w in status_words if w in ("beta", "waitlist", "coming soon", "early access", "invite-only", "invite only")}
        live = {w for w in status_words if w in ("generally available", "now available")}
        tags = [self.ev.add("claim", p["url"], "Status phrase: " + ph) for p, ph in found[:5]]
        if stages and live:
            self.add("MSG-04", WARN, "Pages call the product both %s and %s." % ("/".join(sorted(stages)), "/".join(sorted(live))), tags)
        else:
            self.add("MSG-04", INFO if found else PASS, ("Status words: %s." % ", ".join(sorted(status_words))) if found else "No launch-status words found.", tags)

    def content(self) -> None:
        # CNT-01..CNT-06: titles, descriptions, thin pages, question headings, dates, comparison pages.
        missing = [p for p in self.pages if not p.get("title") or not p.get("description")]
        tags = [self.ev.add("page", p["url"], "title=%s; meta description=%s" % (bool(p.get("title")), bool(p.get("description")))) for p in missing[:5]]
        self.add("CNT-01", WARN if missing else PASS, "%d of %d sampled pages lack a title or meta description." % (len(missing), len(self.pages)), tags)
        titles = Counter(p.get("title") for p in self.pages if p.get("title"))
        dupes = [t for t, c in titles.items() if c > 1]
        self.add("CNT-02", WARN if dupes else PASS, ("%d titles repeat across pages." % len(dupes)) if dupes else "Titles are unique.", [self.ev.add("page", self.origin, "Repeated title: %s" % t) for t in dupes[:3]])
        others = [p for p in self.pages if p is not self.facts.get("home")]
        thin = [p for p in others if p["words"] < 200]
        tags = [self.ev.add("page", p["url"], "%d words of visible text" % p["words"]) for p in thin[:5]]
        self.add("CNT-03", WARN if others and len(thin) > 0.3 * len(others) else PASS, "%d of %d sampled pages have under 200 words." % (len(thin), len(others)), tags)
        headings = [h for p in self.pages for h in p.get("h2", [])]
        questions = [h for h in headings if QUESTION_RE.search(h.strip())]
        self.add("CNT-04", INFO, "%d of %d h2 headings are phrased as questions." % (len(questions), len(headings)), [])
        articles = [p for p in self.pages if ARTICLE_RE.search(urllib.parse.urlsplit(p["url"]).path)]
        undated = [p for p in articles if not p.get("times") and not DATE_TEXT_RE.search(p.get("text_sample", ""))]
        if not articles:
            self.add("CNT-05", NOT_CHECKED, "No article pages sampled.")
        else:
            self.add("CNT-05", WARN if undated else PASS, "%d of %d sampled articles show no date near the top." % (len(undated), len(articles)), [self.ev.add("page", p["url"], "No <time> element or written date in the first 1,500 characters") for p in undated[:3]])
        compare = [loc for loc, _ in self.facts.get("sitemap_urls", [])] + [p["url"] for p in self.facts.get("pages", [])]
        hits = sorted({u for u in compare if re.search(r"(^|[/_-])(vs|versus|alternatives?|compare|comparison)([/_-]|$)", urllib.parse.urlsplit(u).path.lower())})
        self.add("CNT-06", INFO, ("%d comparison or alternatives URLs found." % len(hits)) if hits else "No comparison or alternatives pages found.", [self.ev.add("page", self.origin, "Comparison URLs: " + ", ".join(hits[:5]))] if hits else [])

    def discovery(self) -> None:
        # DSC-01..DSC-07 and MEA-04: well-known files, feeds, Link header, verification tags. All optional; none is a ranking lever.
        rows = {r["path"]: r for r in self.facts.get("well_known", [])}
        self._security_txt(rows.get("/.well-known/security.txt", {}))
        card = rows.get("/.well-known/mcp/server-card.json", {})
        self.add("DSC-02", PASS if card.get("real") else INFO, "An MCP server card is published." if card.get("real") else "No MCP server card at /.well-known/mcp/server-card.json (relevant only if the product has an MCP server; the registry is the main route).", [])
        feeds = [f for p in self.pages for f in p.get("feeds", [])]
        self.add("DSC-03", PASS if feeds else INFO, ("Feed linked: " + feeds[0]) if feeds else "No RSS or Atom feed linked from the sampled pages.", [])
        ard, legacy = rows.get("/.well-known/ard.json", {}), rows.get("/.well-known/ai-catalog.json", {})
        if ard.get("real"):
            self.add("DSC-04", PASS, "/.well-known/ard.json is published.", [])
        elif legacy.get("real"):
            self.add("DSC-04", WARN, "Only the legacy /.well-known/ai-catalog.json exists; ARD v0.91 and Lighthouse expect /.well-known/ard.json.", [self.ev.add("dsc", self.origin + "/.well-known/ai-catalog.json", "Legacy AI catalog found; no ard.json")])
        else:
            self.add("DSC-04", INFO, "No agent resource manifest (experimental, optional).", [])
        api = rows.get("/.well-known/api-catalog", {})
        if api.get("real"):
            linkset = "linkset" in (api.get("content_type") or "")
            self.add("DSC-05", PASS if linkset else WARN, "API catalog published." if linkset else "The API catalog is not served as application/linkset+json (RFC 9727).", [self.ev.add("dsc", self.origin + "/.well-known/api-catalog", "Content-Type: %s" % api.get("content_type"))])
        else:
            self.add("DSC-05", INFO, "No /.well-known/api-catalog (relevant only for products with a public API).", [])
        skills = [p for p in ("/.well-known/agent-skills/index.json", "/.well-known/skills/index.json") if rows.get(p, {}).get("real")]
        self.add("DSC-06", PASS if skills else INFO, ("Agent skills index at " + ", ".join(skills)) if skills else "No agent skills index (optional, developer products).", [])
        link = ((self.facts.get("home") or {}).get("link_header") or "")
        rels = sorted(set(re.findall(r"rel=\"?([a-z0-9-]+)", link, re.I)) & {"llms-txt", "llms-full-txt", "api-catalog", "ard", "service-meta", "service-desc", "alternate", "mcp-server-card", "agent-skills"})
        self.add("DSC-07", PASS if rels else INFO, ("The home Link header advertises: " + ", ".join(rels)) if rels else "The home page sends no Link header pointing at agent resources.", [])
        verified = (self.facts.get("home") or {}).get("verification", [])
        self.add("MEA-04", PASS if "msvalidate.01" in verified else INFO, ("Verification tags on the home page: " + ", ".join(verified)) if verified else "No Bing (msvalidate.01) or Google verification meta tag; DNS verification is also possible, so ask the owner.", [])

    def _security_txt(self, row: dict) -> None:
        # DSC-01: Contact present and exactly one Expires that is in the future and under a year away.
        if not row.get("real"):
            self.add("DSC-01", INFO, "No /.well-known/security.txt (trust hygiene only; no documented AEO effect).", [])
            return
        text = row.get("head", "")
        contact = re.search(r"(?im)^contact:\s*\S+", text)
        expires = re.findall(r"(?im)^expires:\s*(\S+)", text)
        problem = None
        if not contact:
            problem = "no Contact line"
        elif len(expires) != 1:
            problem = "%d Expires lines (exactly one is required)" % len(expires)
        else:
            try:
                when = dt.date.fromisoformat(expires[0][:10])
                if when < self.today:
                    problem = "Expires %s is in the past" % expires[0]
                elif (when - self.today).days > 366:
                    problem = "Expires %s is more than a year away" % expires[0]
            except ValueError:
                problem = "Expires %r is not an RFC 3339 date" % expires[0]
        tag = self.ev.add("dsc", self.origin + "/.well-known/security.txt", "security.txt: " + (problem or "valid"))
        self.add("DSC-01", WARN if problem else PASS, ("security.txt has " + problem + ".") if problem else "security.txt is valid.", [tag])

    def reachability(self) -> None:
        # ACC-10 soft 404s, ACC-11 inherited rules, ACC-16 crawl-delay, REN-05 HTML size, REN-06 plain links, IDX-05 noarchive.
        missing = self.facts.get("missing_page") or {}
        if missing.get("status"):
            soft = missing["status"] < 400
            tag = self.ev.add("page", missing["url"], "A made-up URL answered %s%s." % (missing["status"], (" after redirecting to " + missing["final_url"]) if missing.get("redirects") else ""))
            self.add("ACC-10", FAIL if soft else PASS, "A page that does not exist answers %s, so crawlers can index error pages as content." % missing["status"] if soft else "Missing pages answer %s." % missing["status"], [tag])
        else:
            self.add("ACC-10", NOT_CHECKED, "The missing-page probe got no answer.")
        if self.robots.names("googlebot") and not self.robots.names("applebot"):
            blocked = not self.robots.allowed("Applebot", "/")
            tag = self.ev.add("robots", self.origin + "/robots.txt", "robots.txt names Googlebot but not Applebot; Applebot follows the Googlebot group (home allowed: %s)." % (not blocked))
            self.add("ACC-11", WARN if blocked else INFO, "Applebot follows your Googlebot rules because robots.txt does not name it%s." % (", and they block it from the home page" if blocked else ""), [tag])
        else:
            self.add("ACC-11", PASS, "No bot silently inherits another bot's group.", [])
        robots_text = self.facts.get("robots", {}).get("text", "")
        delays = re.findall(r"(?im)^\s*crawl-delay\s*:\s*(\S+)", robots_text)
        self.add("ACC-16", INFO if delays else PASS, ("Crawl-delay %s is set; Google, Apple, and Amazon ignore it, and bots that honor it slow down a lot." % ", ".join(delays[:3])) if delays else "No Crawl-delay lines.", [])
        big = [p for p in self.facts.get("pages", []) if p.get("bytes", 0) > 2_000_000 or p.get("truncated")]
        tags = [self.ev.add("page", p["url"], "HTML is %s bytes" % ("over 3,000,000" if p.get("truncated") else p["bytes"])) for p in big[:3]]
        self.add("REN-05", WARN if big else PASS, ("%d pages exceed 2 MB of HTML; Googlebot reads only the first 2 MB." % len(big)) if big else "Every sampled page is under 2 MB of HTML.", tags)
        home = self.facts.get("home") or {}
        links = home.get("internal_links", 0)
        if home.get("status") and home["status"] < 400:
            self.add("REN-06", PASS if links >= 5 else WARN, "The home page has %d same-site <a href> links in its raw HTML%s." % (links, "" if links >= 5 else "; crawlers that do not run JavaScript cannot find the other pages"), [self.ev.add("page", home["url"], "%d same-site <a href> links in raw HTML" % links)])
        else:
            self.add("REN-06", NOT_CHECKED, "The home page did not load.")
        archived = [p for p in self.pages if "noarchive" in " ".join(filter(None, [p.get("robots_meta"), p.get("x_robots_tag")])).lower()]
        atags = [self.ev.add("page", p["url"], "noarchive in %s" % (p.get("robots_meta") or p.get("x_robots_tag"))) for p in archived[:3]]
        self.add("IDX-05", FAIL if archived else PASS, ("%d pages carry noarchive; Bing keeps NOARCHIVE pages out of Copilot answers and grounding." % len(archived)) if archived else "No noarchive directives.", atags)

    def schema_facts(self) -> None:
        # SCH-09 marked-up prices, SCH-10 retired types, SCH-11 authors, SCH-12 Organization completeness.
        mismatches = []
        for p in self.pages:
            visible = {Amounts.normalize(v) for v in p["claims"]["prices"]}
            for node in p.get("jsonld", []):
                for price in OfferPrices.of(node):
                    amount = Amounts.normalize(price)
                    if visible and amount not in visible and not (amount == "0" and p["claims"]["free"]):
                        mismatches.append((p, price, sorted(visible)[:6]))
        tags = [self.ev.add("schema", p["url"], "Markup price %s; visible prices %s" % (price, ", ".join(vis))) for p, price, vis in mismatches[:4]]
        has_offers = any(OfferPrices.of(n) for p in self.pages for n in p.get("jsonld", []))
        self.add("SCH-09", FAIL if mismatches else (PASS if has_offers else NOT_CHECKED), ("%d marked-up prices do not appear on their page." % len(mismatches)) if mismatches else ("Marked-up prices appear on their pages." if has_offers else "No Offer prices in markup."), tags)
        retired = sorted({(t, RETIRED_TYPES[t]) for p in self.pages for n in p.get("jsonld", []) for t in JsonLd.types(n) if t in RETIRED_TYPES})
        self.add("SCH-10", INFO if retired else PASS, ("Markup uses types with no Google feature: " + "; ".join("%s (%s)" % r for r in retired)) if retired else "No retired rich-result types.", [])
        generic = []
        for p in self.pages:
            for node in p.get("jsonld", []):
                if {"Article", "BlogPosting", "NewsArticle", "TechArticle"} & set(JsonLd.types(node)):
                    for author in node.get("author") if isinstance(node.get("author"), list) else [node.get("author")]:
                        name = (author.get("name") if isinstance(author, dict) else author) or ""
                        if not str(name).strip() or str(name).strip().lower() in GENERIC_AUTHORS:
                            generic.append((p, name or "(none)"))
        gtags = [self.ev.add("schema", p["url"], "Article author: %s" % n) for p, n in generic[:3]]
        self.add("SCH-11", WARN if generic else PASS, ("%d articles name no real author (e.g. '%s')." % (len(generic), generic[0][1])) if generic else "Article authors are named.", gtags)
        orgs = OrgNodes.of(self.facts.get("home") or {})
        if not orgs:
            self.add("SCH-12", NOT_CHECKED, "No Organization node on the home page.")
            return
        missing = [k for k in ("url", "logo", "description") if not orgs[0].get(k)]
        self.add("SCH-12", WARN if missing else PASS, ("The Organization node lacks: " + ", ".join(missing)) if missing else "The Organization node has url, logo, and description.", [self.ev.add("schema", self.origin + "/", "Organization keys: " + ", ".join(sorted(k for k in orgs[0] if not k.startswith("@"))))])


class OfferPrices:
    """Finds the literal prices a node's offers state."""

    @staticmethod
    def of(node: dict) -> list:
        # offers.price / lowPrice / highPrice values (offers may be one dict or a list), as strings.
        offers = node.get("offers")
        if "Offer" in JsonLd.types(node) or "AggregateOffer" in JsonLd.types(node):
            offers = node
        out = []
        for offer in offers if isinstance(offers, list) else [offers]:
            if isinstance(offer, dict):
                for key in ("price", "lowPrice", "highPrice"):
                    if isinstance(offer.get(key), (str, int, float)) and str(offer.get(key)).strip():
                        out.append("$" + str(offer[key]))
        return out


class JsonValues:
    """Walks JSON-LD values."""

    IDENTITY_KEYS = {"name", "alternateName", "legalName", "url", "@id", "logo", "image", "email", "telephone", "sameAs", "address", "streetAddress", "addressLocality", "publisher", "author", "brand", "founder"}

    @classmethod
    def identity_strings(cls, nodes: list) -> list:
        # Strings under identity keys only (names, URLs, logos, contacts), so article text that mentions "Acme" is not a leftover.
        out, stack = [], [v for n in nodes for k, v in n.items() if k in cls.IDENTITY_KEYS]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(v for k, v in item.items() if k in cls.IDENTITY_KEYS)
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                out.append(item)
        return out

    @staticmethod
    def strings(nodes: list) -> list:
        # Every string value inside nodes, depth-first, skipping @context.
        out, stack = [], list(nodes)
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(v for k, v in item.items() if k != "@context")
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                out.append(item)
        return out
