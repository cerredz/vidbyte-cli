#!/usr/bin/env python3
"""Verification script for skills/improve-my-aeo (design doc section 10).

Unit tests import the skill's modules directly. Integration tests serve two fixture sites from a
local HTTP server on 127.0.0.1 (a "broken" site seeded with the failures the audit must catch, and
a "clean" site that must produce no failure) and run aeo_audit.py in subprocesses. Every suggest
and feedback call goes to a recording fake vidbyte-cli passed with --cli; PATH holds no
vidbyte-cli, so an ignored --cli fails with 127 instead of reaching a real (billing) CLI. Nothing
leaves the machine. Run it with every Python you support:

    python scripts/test-improve-my-aeo.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "improve-my-aeo"
SCRIPTS = SKILL / "scripts"
ENTRY = SCRIPTS / "aeo_audit.py"
RUBRIC = SKILL / "references" / "aeo-rubric.md"
SKILL_MD = SKILL / "SKILL.md"
CONTRACT_SECTIONS = ["The problem", "How vidbyte-cli solves it", "Goal and done-when", "Preflight", "Gather inputs", "Steps", "CLI commands",
                     "Cost and consent", "Paid work boundary", "Run", "Read the result", "Deliver", "Troubleshooting", "Guardrails"]
BOT_CATEGORIES = {"search", "training", "user", "agent", "preview", "ads", "other", "infra", "catch-all"}
CHECKLIST = SKILL / "references" / "aeo-checklist.md"
sys.path.insert(0, str(SCRIPTS))

from aeokit.checks import CHECKS, CheckSuite  # noqa: E402
from aeokit.fetch import HttpFetcher  # noqa: E402
from aeokit.html import PageExtractor  # noqa: E402
from aeokit.probe import BOTS, CORE_BOTS, BotRegistry  # noqa: E402
from aeokit.robots import RobotsTxt  # noqa: E402
from aeokit.runner import Redactor  # noqa: E402
from aeokit.suggest import AeoRun  # noqa: E402

TODAY = dt.date.today().isoformat()
STRIP_ENV = ("VIDBYTE_API_KEY", "PYTHONPATH")
FILLER = " ".join(["Acme Cloud runs managed Postgres databases for small product teams with daily backups and point in time recovery."] * 20)

FAKE_CLI_SOURCE = r'''
import json, os, sys

def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    code = int(os.environ.get("FAKE_CLI_EXIT", "0") or 0)
    if "--dry-run" in argv and os.environ.get("FAKE_CLI_DRY_EXIT"):
        code = int(os.environ["FAKE_CLI_DRY_EXIT"])
    if argv[:5] == ["--json", "agents", "suggest", "project", "list"] and not code:
        keys = [k for k in os.environ.get("FAKE_CLI_PROJECTS", "").split(",") if k]
        print(json.dumps({"schema_version": 1, "kind": "suggestions.projects", "data": {"projects": [{"key": k, "title": k} for k in keys]}}))
        return 0
    if code:
        sys.stderr.write(json.dumps({"kind": "error", "data": {"code": "insufficient_credits", "message": "Out of credits.", "hint": "Top up first.", "retryable": False}}) + "\n")
        return code
    if argv[:6] == ["--json", "--no-input", "agents", "suggest", "run", "--goal"]:
        dry = "--dry-run" in argv
        ideas = [] if dry else [
            {"id": "idea-001", "rank": 1, "title": "Let GPTBot-style agents past the firewall", "summary": "[E1] shows a 403; see ACC-05.", "first_action": "Allow the user agent.", "completion_criteria": "aeo_audit.py gather reports ACC-05 pass"},
            {"id": "idea-002", "rank": 2, "title": "Write better content", "summary": "General advice with no tags."},
            {"id": "idea-003", "rank": 3, "title": "Cite a missing fact", "summary": "See [E999] and XYZ-99."},
        ]
        usage = {"model_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0} if dry else {"model_calls": 5, "input_tokens": 200000, "cached_input_tokens": 150000, "output_tokens": 30000}
        data = {"status": "no_suggestions" if dry else "complete", "stop_reason": "dry_run" if dry else "completed", "settings": {"model": "gpt-6-sol", "critic_model": "gpt-6-sol"}, "usage": usage, "ideas": ideas, "context_manifest": [{"ref": "ctx-001", "kind": "file", "status": "included"}], "warnings": []}
        print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": data}))
        return 0
    print(json.dumps({"schema_version": 1, "kind": "ok", "data": {}}))
    return 0
'''


class Results:
    """Collects PASS/FAIL per test and prints the summary."""

    def __init__(self) -> None:
        # Starts with no results.
        self.passed = 0
        self.failed: list = []

    def run(self, name: str, category: str, test) -> None:
        # Runs one test callable; an AssertionError or any exception is a FAIL with its reason.
        try:
            test()
        except Exception as error:  # Every failure, including crashes, is reported, not raised.
            self.failed.append(name)
            print("FAIL [%s] %s -- %s" % (category, name, str(error) or type(error).__name__))
            if not isinstance(error, AssertionError):
                traceback.print_exc()
            return
        self.passed += 1
        print("PASS [%s] %s" % (category, name))

    def finish(self) -> int:
        # Prints "X/Y tests passed" and returns the process exit code.
        total = self.passed + len(self.failed)
        print("\n%d/%d tests passed (Python %s)" % (self.passed, total, sys.version.split()[0]))
        return 0 if not self.failed else 1


class Check:
    """Small assertion helpers with readable messages."""

    @staticmethod
    def equal(actual, expected, label: str = "") -> None:
        # Fails with both values when they differ.
        assert actual == expected, "%s expected %r, got %r" % (label, expected, actual)

    @staticmethod
    def true(value, label: str) -> None:
        # Fails with the label when value is falsy.
        assert value, label


class Pages:
    """HTML builders for the fixture sites."""

    @staticmethod
    def html(title: str, h1: str, body: str, head: str = "", jsonld: list = ()) -> str:
        # One HTML document with optional extra head tags and JSON-LD blocks.
        blocks = "".join('<script type="application/ld+json">%s</script>' % (j if isinstance(j, str) else json.dumps(j)) for j in jsonld)
        return "<!doctype html><html><head><title>%s</title>%s%s</head><body>%s<main><h1>%s</h1>%s</main></body></html>" % (title, head, blocks, Pages.nav(), h1, body)

    @staticmethod
    def nav() -> str:
        # Six same-site links, so the home page passes the plain-links check.
        return "<nav>" + "".join('<a href="%s">%s</a>' % (p, p) for p in ("/pricing", "/about", "/blog/post-one", "/app", "/docs", "/contact")) + "</nav>"


class BrokenSite:
    """Routes for a site seeded with the failures the audit must report."""

    def __init__(self, origin: str) -> None:
        # origin is http://127.0.0.1:<port>.
        self.origin = origin

    def route(self, path: str, ua: str, accept: str):
        # Returns (status, headers, body) for one request.
        if path == "/robots.txt":
            groups = "User-agent: *\nAllow: /\n\nUser-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n\nUser-agent: CCBot\nDisallow: /\n\nUser-agent: AdsBot-Google\nDisallow: /\n\n"
            return 200, {"Content-Type": "text/plain"}, groups + "Sitemap: %s/sitemap.xml\n" % self.origin
        if path == "/sitemap.xml":
            urls = "".join("<url><loc>%s%s</loc><lastmod>2026-03-01</lastmod></url>" % (self.origin, p) for p in ("/", "/pricing", "/about", "/blog/post-one", "/app"))
            return 200, {"Content-Type": "application/xml"}, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % urls
        if path == "/llms.txt":
            text = "<!-- internal: generated by src/lib/llms.js -->\n# Acme Cloud\n\nSend Accept: text/markdown to get Markdown.\nTeam plan is $59 per month.\n\n## Docs\n- [Pricing](/pricing): plans\n- [Gone](/missing-page-xyz): old\n"
            return 200, {"Content-Type": "text/plain"}, text
        if path == "/" and ("GPTBot" in ua or "facebookexternalhit" in ua):
            return 403, {"Content-Type": "text/html"}, "<html><body>Forbidden</body></html>"
        if path == "/":
            org = {"@context": "https://schema.org", "@type": "Organization", "name": "ChatPack", "url": "https://oldbrand.example/", "dateModified": TODAY}
            faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": "Can I self host the hidden enterprise edition?", "acceptedAnswer": {"@type": "Answer", "text": "Yes."}}]}
            return 200, {"Content-Type": "text/html"}, Pages.html("Home | Acme Cloud", "Managed Postgres", "<p>%s</p>" % FILLER, '<meta property="og:site_name" content="Acme Cloud">', [org, faq])
        if path == "/pricing":
            return 200, {"Content-Type": "text/html"}, Pages.html("Pricing | Acme Cloud", "Pricing", "<p>Starter $49 per month. Team $99 per month. %s</p>" % FILLER, "", [{"@type": "WebPage", "dateModified": TODAY}])
        if path == "/about":
            return 200, {"Content-Type": "text/html"}, Pages.html("About | Acme Cloud", "About", "<p>%s</p>" % FILLER, '<meta name="robots" content="noindex">', [{"@type": "AboutPage", "dateModified": TODAY}])
        if path == "/blog/post-one":
            article = {"@type": "BlogPosting", "headline": "Post", "author": {"@type": "Person", "name": "Admin"}, "datePublished": "2026-01-01"}
            return 200, {"Content-Type": "text/html"}, Pages.html("Post | Acme Cloud", "Post", "<p>%s</p>" % FILLER, '<meta name="robots" content="noarchive">', [article])
        if path == "/app":
            return 200, {"Content-Type": "text/html"}, '<!doctype html><html><head><title>App | Acme Cloud</title></head><body><div id="root"></div><script src="/_next/static/app.js"></script></body></html>'
        if path == "/index.html.md":
            return 200, {"Content-Type": "text/markdown"}, "# Acme Cloud\n\nManaged Postgres.\n"
        if path == "/loop":
            return 302, {"Location": "/loop"}, ""
        if path == "/missing-page-xyz":
            return 404, {"Content-Type": "text/html"}, "<html><body>Not found</body></html>"
        return 200, {"Content-Type": "text/html"}, "<html><body><h1>Oops</h1>Page not found</body></html>"


class CleanSite:
    """Routes for a site that should produce no failing check."""

    def __init__(self, origin: str) -> None:
        # origin is http://127.0.0.1:<port>.
        self.origin = origin

    def route(self, path: str, ua: str, accept: str):
        # Returns (status, headers, body) for one request.
        o = self.origin
        if path == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % o
        if path == "/sitemap.xml":
            dates = ("2026-01-05", "2026-02-11", "2026-03-20", "2026-04-02", "2026-05-17", "2026-06-30")
            urls = "".join("<url><loc>%s%s</loc><lastmod>%s</lastmod></url>" % (o, p, d) for p, d in zip(("/", "/pricing", "/about", "/docs", "/contact", "/blog/post-one"), dates))
            return 200, {"Content-Type": "application/xml"}, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % urls
        if path == "/llms.txt":
            text = "# Acme Cloud\n\n> Acme Cloud runs managed Postgres for small product teams, with backups and point-in-time recovery.\n\n## Docs\n- [Pricing](%s/pricing.md): plans and limits\n- [About](%s/about.md): the company\n- [Overview](%s/index.html.md): what it does\n" % (o, o, o)
            return 200, {"Content-Type": "text/plain"}, text
        pages = {"/": ("Acme Cloud", "Managed Postgres"), "/pricing": ("Pricing | Acme Cloud", "Pricing"), "/about": ("About | Acme Cloud", "About"),
                 "/docs": ("Docs | Acme Cloud", "Docs"), "/contact": ("Contact | Acme Cloud", "Contact"), "/blog/post-one": ("Backups explained | Acme Cloud", "Backups explained"),
                 "/app": ("Console | Acme Cloud", "Console")}
        md = {"/index.html.md": "/", "/pricing.md": "/pricing", "/about.md": "/about", "/docs.md": "/docs", "/contact.md": "/contact", "/blog/post-one.md": "/blog/post-one", "/app.md": "/app"}
        if path in md:
            return 200, {"Content-Type": "text/markdown", "Link": '<%s%s>; rel="canonical"' % (o, md[path])}, "# %s\n\nSee [llms.txt](/llms.txt).\n\n%s\n" % (pages[md[path]][1], FILLER[:400])
        if path in pages and "text/markdown" in accept and path == "/":
            return 200, {"Content-Type": "text/markdown", "Vary": "Accept"}, "# Managed Postgres\n\n%s\n" % FILLER[:400]
        if path in pages:
            title, h1 = pages[path]
            mirror = "/index.html.md" if path == "/" else path + ".md"
            head = '<meta name="description" content="%s for small product teams."><meta property="og:site_name" content="Acme Cloud"><link rel="canonical" href="%s%s"><link rel="alternate" type="text/markdown" href="%s"><link rel="alternate" type="application/rss+xml" href="/feed.xml">' % (h1, o, path, mirror)
            jsonld = []
            if path == "/":
                jsonld = [{"@context": "https://schema.org", "@type": "Organization", "name": "Acme Cloud", "url": o + "/", "logo": o + "/logo.png", "description": "Managed Postgres", "sameAs": ["https://github.com/acme-cloud"]},
                          {"@context": "https://schema.org", "@type": "WebSite", "name": "Acme Cloud", "url": o + "/"}]
            if path == "/blog/post-one":
                jsonld = [{"@type": "BlogPosting", "headline": h1, "author": {"@type": "Person", "name": "Dana Lee"}, "datePublished": "2026-01-02", "dateModified": "2026-02-03"}]
            body = "<p>Published 2026-01-02.</p>" if path.startswith("/blog/") else ""
            body += "<p>Starter $49 per month. Team $99 per month.</p>" if path in ("/", "/pricing") else ""
            return 200, {"Content-Type": "text/html", "Vary": "Accept"}, Pages.html(title, h1, body + "<p>%s</p>" % FILLER, head, jsonld)
        return 404, {"Content-Type": "text/html"}, "<html><body>Not found</body></html>"


class FixtureServer:
    """A local HTTP server whose routes come from a site object; one per test run."""

    def __init__(self, site_class) -> None:
        # Binds port 0 on 127.0.0.1 and serves in a daemon thread.
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.httpd.site = site_class(self.origin)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def _handler():
        # A request handler that asks the site object for (status, headers, body).
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server hook name
                status, headers, body = self.server.site.route(self.path.split("?")[0], self.headers.get("User-Agent", ""), self.headers.get("Accept", ""))
                data = body.encode("utf-8")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # Silence the per-request log.
                pass
        return Handler

    def close(self) -> None:
        # Stops the server.
        self.httpd.shutdown()


class FakeCli:
    """A recording fake vidbyte-cli: a real launcher (an .exe on Windows) made with pip's distlib."""

    def __init__(self, base: Path) -> None:
        # Writes fakecli.py and builds the launcher in base/fakebin.
        self.module_dir = base / "fakecli"
        self.module_dir.mkdir(parents=True, exist_ok=True)
        (self.module_dir / "fakecli.py").write_text(FAKE_CLI_SOURCE, encoding="utf-8")
        self.bin_dir = base / "fakebin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.log = base / "fakecli-log.jsonl"
        from pip._vendor.distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, str(self.bin_dir))
        maker.executable = sys.executable
        maker.variants = {""}
        self.path = maker.make("vidbyte-cli = fakecli:main")[0]

    def calls(self) -> list:
        # Every recorded call: {"argv": [...], "cwd": "..."}.
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]


class Runner:
    """Runs aeo_audit.py in a subprocess with a PATH that holds no vidbyte-cli."""

    def __init__(self, fake: FakeCli) -> None:
        # Builds the confined environment once.
        self.fake = fake
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.update({"FAKE_CLI_LOG": str(fake.log), "PYTHONPATH": str(fake.module_dir), "PYTHONIOENCODING": "utf-8", "PATH": self._clean_path()})
        self.env = env

    def run(self, *args: str, extra_env: dict = None):
        # Returns (exit code, parsed stdout JSON or None, stdout text, stderr text).
        env = dict(self.env)
        env.update(extra_env or {})
        process = subprocess.run([sys.executable, str(ENTRY)] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        out = process.stdout.decode("utf-8", "replace")
        try:
            document = json.loads(out) if out.strip() else None
        except ValueError:
            document = None
        return process.returncode, document, out, process.stderr.decode("utf-8", "replace")

    @staticmethod
    def _clean_path() -> str:
        # The current PATH minus every folder that holds a vidbyte-cli.
        keep = [f for f in os.environ.get("PATH", "").split(os.pathsep) if f and not any(os.path.exists(os.path.join(f, n)) for n in ("vidbyte-cli", "vidbyte-cli.exe", "vidbyte-cli.cmd"))]
        return os.pathsep.join(keep)


class UnitTests:
    """Tests that import the skill's modules directly."""

    def __init__(self, results: Results) -> None:
        # Shares the result collector.
        self.r = results

    def run(self) -> None:
        # Every unit test, labelled with its failure category.
        r = self.r
        r.run("RobotsTxt: longest match wins between Allow and Disallow", "Silent Failure", self.longest_match)
        r.run("RobotsTxt: a named group replaces *, an unnamed agent falls back to *", "Hidden Assumption", self.group_selection)
        r.run("RobotsTxt: 404 and empty allow all, 5xx and no answer disallow all", "Edge Case", self.status_rules)
        r.run("RobotsTxt: * wildcard and $ anchor follow RFC 9309", "Edge Case", self.wildcards)
        r.run("RobotsTxt: agent tokens are case-insensitive and comments are ignored", "Hidden Assumption", self.case_and_comments)
        r.run("RobotsTxt: Applebot follows the Googlebot group when not named", "Hidden Assumption", self.applebot_inherits)
        r.run("RobotsTxt: an HTML page served as robots.txt holds no rules", "Hidden Failure", self.html_robots)
        r.run("PageExtractor: @graph is flattened and invalid JSON-LD is recorded, not dropped", "Silent Failure", self.jsonld)
        r.run("PageExtractor: script, style, and noscript text is not counted", "Silent Failure", self.visible_text)
        r.run("PageExtractor: a page with no head or title yields empty facts", "Edge Case", self.empty_page)
        r.run("Redactor: keys and secret assignments are removed", "Hidden Failure", self.redactor)
        r.run("InputsValidator: untagged and unknown tags are refused, checklist ids accepted", "Hidden Assumption", self.validator_tags)
        r.run("InputsValidator: more than 3 questions or a goal over 700 characters is refused", "Edge Case", self.validator_limits)
        r.run("aeo-rubric.md fits the CLI's 20,000-byte instructions limit", "Silent Failure", self.rubric_size)
        r.run("Every checklist id is explained in the rubric, and the rubric names no unknown id", "Silent Failure", self.rubric_coverage)
        r.run("Every script check id is a checklist item", "Silent Failure", self.script_ids_in_checklist)
        r.run("Every checklist item has a Check, an Audit kind, and a Confidence line", "Silent Failure", self.checklist_shape)
        r.run("Every checklist cross-reference names a real item", "Silent Failure", self.checklist_refs)
        r.run("CheckSuite: question-only mode marks every check not_checked", "Edge Case", self.not_audited)
        r.run("BotRegistry: 66 source-dated bots in the nine categories, every core bot present", "Hidden Assumption", self.registry_shape)
        r.run("BotRegistry: robots-only tokens never fetch, and a token-only bot gets a built user agent", "Edge Case", self.registry_fetchers)
        r.run("BotRegistry: each robots.txt token is evaluated once (BingPreview reuses bingbot)", "Edge Case", self.registry_tokens)
        r.run("Checklist: SEO-01 to SEO-22 exist and every registry bot is in the Bot list", "Silent Failure", self.seo_and_bot_list)
        r.run("SKILL.md: every contract section in order, body under 10,000 bytes, description verbatim-safe", "Hidden Assumption", self.skill_contract)

    def longest_match(self) -> None:
        robots = RobotsTxt("User-agent: *\nDisallow: /docs\nAllow: /docs/public\n")
        Check.equal(robots.allowed("GPTBot", "/docs/public/a"), True, "longer Allow")
        Check.equal(robots.allowed("GPTBot", "/docs/private"), False, "Disallow")
        tie = RobotsTxt("User-agent: *\nDisallow: /a\nAllow: /a\n")
        Check.equal(tie.allowed("x", "/a"), True, "allow wins a tie")

    def group_selection(self) -> None:
        robots = RobotsTxt("User-agent: *\nDisallow: /\n\nUser-agent: OAI-SearchBot\nAllow: /\n")
        Check.equal(robots.allowed("OAI-SearchBot", "/"), True, "named group")
        Check.equal(robots.allowed("GPTBot", "/"), False, "falls back to *")
        only_named = RobotsTxt("User-agent: GPTBot\nDisallow: /\n")
        Check.equal(only_named.allowed("ClaudeBot", "/x"), True, "no group applies")

    def status_rules(self) -> None:
        Check.equal(RobotsTxt("", 404).allowed("GPTBot", "/"), True, "404")
        Check.equal(RobotsTxt("", 200).allowed("GPTBot", "/"), True, "empty")
        Check.equal(RobotsTxt("User-agent: *\nAllow: /", 503).allowed("GPTBot", "/"), False, "503")
        Check.equal(RobotsTxt("", 0).unreachable, True, "no answer")

    def wildcards(self) -> None:
        robots = RobotsTxt("User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*/x\n")
        Check.equal(robots.allowed("a", "/files/report.pdf"), False, "$ anchored match")
        Check.equal(robots.allowed("a", "/files/report.pdf?x=1"), True, "$ blocks only the exact end")
        Check.equal(robots.allowed("a", "/tmp123/x"), False, "* wildcard")

    def case_and_comments(self) -> None:
        robots = RobotsTxt("# comment\nUser-Agent: gptbot  # trailing\nDisallow: /private # note\n")
        Check.equal(robots.allowed("GPTBot", "/private/a"), False, "case-insensitive token")
        Check.equal(robots.allowed("GPTBot", "/public"), True, "comment not part of path")

    def applebot_inherits(self) -> None:
        robots = RobotsTxt("User-agent: *\nAllow: /\n\nUser-agent: Googlebot\nDisallow: /\n")
        Check.equal(robots.allowed("Applebot", "/"), False, "Applebot follows Googlebot")
        named = RobotsTxt("User-agent: Googlebot\nDisallow: /\n\nUser-agent: Applebot\nAllow: /\n")
        Check.equal(named.allowed("Applebot", "/"), True, "named Applebot uses its own group")

    def html_robots(self) -> None:
        robots = RobotsTxt("<!doctype html><html><body>User-agent: *\nDisallow: /</body></html>", 200, "text/html")
        Check.true(robots.served_as_html, "flagged as HTML")
        Check.equal(robots.groups, [], "no groups parsed")

    def jsonld(self) -> None:
        html = '<html><head><script type="application/ld+json">{"@graph":[{"@type":"Organization","name":"A"},{"@type":"WebSite"}]}</script><script type="application/ld+json">{bad json</script></head><body>x</body></html>'
        facts = PageExtractor.extract(html)
        Check.equal(sorted(n["@type"] for n in facts["jsonld"]), ["Organization", "WebSite"], "flattened")
        Check.equal(len(facts["jsonld_errors"]), 1, "parse error kept")

    def visible_text(self) -> None:
        facts = PageExtractor.extract("<html><body><script>var a = 'one two three';</script><style>.x{}</style><noscript>four five</noscript><p>six seven</p></body></html>")
        Check.equal(facts["words"], 2, "only visible words")

    def empty_page(self) -> None:
        facts = PageExtractor.extract("")
        Check.equal((facts["title"], facts["canonical"], facts["words"], facts["h1"]), (None, None, 0, []), "empty facts")

    def redactor(self) -> None:
        red = Redactor()
        out = red("key sk-abcdefghijklmnopqrstuv and api_key=supersecret123 and ghp_abcdefghijklmnopqrstuvwxyz012345")
        Check.true("sk-abc" not in out and "supersecret" not in out and "ghp_" not in out, "secrets removed: %s" % out)
        Check.equal(red.count, 3, "count")

    def validator_tags(self) -> None:
        validator = AeoRun.validator({"E1", "E2", "ACC-02"}, Redactor())
        base = {"goal": "Fix it.", "project": None}
        _, problems = validator.validate(dict(base, mistakes=["no tag here"]))
        Check.true(any("must start with a bracketed" in p for p in problems), "untagged refused: %s" % problems)
        _, problems = validator.validate(dict(base, mistakes=["[E77] unknown"]))
        Check.true(any("E77" in p for p in problems), "unknown tag refused: %s" % problems)
        _, problems = validator.validate(dict(base, mistakes=["[ACC-02 E1] known"], question=["Why?"]))
        Check.equal(problems, [], "known tags accepted")

    def validator_limits(self) -> None:
        validator = AeoRun.validator(set(), Redactor())
        _, problems = validator.validate({"goal": "x" * 701, "project": None, "question": ["a", "b", "c", "d"]})
        Check.true(any("goal has" in p for p in problems), "long goal refused")
        Check.true(any("question has 4 values" in p for p in problems), "4 questions refused: %s" % problems)

    def rubric_size(self) -> None:
        size = RUBRIC.stat().st_size
        Check.true(size <= 20_000, "rubric is %d bytes" % size)

    def rubric_coverage(self) -> None:
        checklist = AeoRun.checklist_ids(CHECKLIST)
        rubric = set(re.findall(r"(?m)^- ([A-Z]{2,3}-\d{2}) ", RUBRIC.read_text(encoding="utf-8")))
        Check.equal(sorted(checklist - rubric), [], "checklist ids missing from the rubric")
        Check.equal(sorted(rubric - checklist), [], "rubric ids missing from the checklist")

    def script_ids_in_checklist(self) -> None:
        Check.equal(sorted(set(CHECKS) - AeoRun.checklist_ids(CHECKLIST)), [], "script ids missing from the checklist")

    def checklist_shape(self) -> None:
        text = CHECKLIST.read_text(encoding="utf-8")
        items = re.split(r"(?m)^### ", text)[1:]
        bad = [i.split("\n")[0] for i in items if "- Check:" not in i or "Audit:" not in i or "- Confidence:" not in i]
        Check.equal(bad, [], "items missing a field")
        # Every model call carries the checklist (mostly cached after the first), so its size is bounded.
        Check.true(len(text.encode("utf-8")) <= 110_000, "checklist is %d bytes" % len(text.encode("utf-8")))

    def checklist_refs(self) -> None:
        text = CHECKLIST.read_text(encoding="utf-8")
        refs = set(re.findall(r"\b([A-Z]{2,3}-\d{2})\b", text))
        Check.equal(sorted(refs - AeoRun.checklist_ids(CHECKLIST)), [], "dangling references")

    def not_audited(self) -> None:
        results = CheckSuite.not_audited("No site.")
        Check.equal({r.status for r in results}, {"not_checked"}, "statuses")
        Check.equal(len(results), len(CHECKS), "one per check")

    def registry_shape(self) -> None:
        Check.equal(len(BOTS), 66, "bot count")
        Check.equal({b["category"] for b in BOTS}, BOT_CATEGORIES, "categories")
        # Catch-all entries (curl, an empty user agent, a made-up bot) test the policy, so they have no operator docs.
        Check.equal([b["name"] for b in BOTS if b["category"] != "catch-all" and (not b.get("checked") or not b.get("sourceUrls"))], [], "bots without a source and date")
        Check.equal(sorted(set(CORE_BOTS) - {b["name"] for b in BOTS}), [], "core bots missing from the registry")

    def registry_fetchers(self) -> None:
        fetchers = {name: ua for name, _, ua in BotRegistry.fetchers(BOTS)}
        Check.true("Google-Extended" not in fetchers and "Applebot-Extended" not in fetchers, "robots-only tokens never fetch")
        Check.equal(len(fetchers), sum(1 for b in BOTS if not b.get("uaOnly")), "one fetch per fetching bot")
        Check.equal(fetchers["ClaudeBot"], "Mozilla/5.0 (compatible; ClaudeBot)", "token-only bot")
        Check.equal(fetchers["Empty user-agent"], "", "empty user agent stays empty")

    def registry_tokens(self) -> None:
        tokens = BotRegistry.robots_tokens(BOTS)
        lowered = [t.lower() for t, _, _ in tokens]
        Check.equal(len(lowered), len(set(lowered)), "no duplicate tokens")
        Check.equal([c for t, c, _ in tokens if t == "bingbot"], ["search"], "bingbot keeps its first category")
        Check.true(("AdsBot-Google", "ads", True) in tokens, "AdsBot-Google obeys only named groups")

    def seo_and_bot_list(self) -> None:
        ids = AeoRun.checklist_ids(CHECKLIST)
        Check.equal(sorted("SEO-%02d" % n for n in range(1, 23) if "SEO-%02d" % n not in ids), [], "missing SEO items")
        bot_list = CHECKLIST.read_text(encoding="utf-8").split("## Bot list", 1)[-1]
        Check.equal([b["name"] for b in BOTS if "| %s |" % b["name"] not in bot_list], [], "bots missing from the Bot list")

    def skill_contract(self) -> None:
        text = SKILL_MD.read_text(encoding="utf-8")
        Check.equal(re.findall(r"(?m)^## (.+)$", text), CONTRACT_SECTIONS, "sections")
        body = text.split("\n---\n", 1)[1].replace("\r\n", "\n")
        # The cap keeps this skill's own instructions minimal; the paid work boundary text that scripts/sync-paid-work-boundary.py writes into every skill is not counted.
        body = re.sub(r"(?ms)^## Paid work boundary\n.*?(?=^## )", "", body)
        body = re.sub(r"(?m)^- Deliver [^\n]*(?:\n  [^\n]*)*\n", lambda m: "" if "(Paid work boundary)." in " ".join(m.group(0).split()) else m.group(0), body)
        body = body.replace("The consent question has two answers. On a no, stop as Paid work boundary says: nothing is spent,\nand nothing else is offered.\n", "")
        Check.true(len(body.encode("utf-8")) < 10_000, "body without the synced boundary is %d bytes" % len(body.encode("utf-8")))
        description = re.search(r"(?m)^description: (.+)$", text).group(1)
        Check.true(description.startswith("Use when") and len(description) <= 1024 and "$" not in description, "description contract")
        Check.true("%d checks" % len(CHECKS) in description and "%d-item" % len(AeoRun.checklist_ids(CHECKLIST)) in description, "description counts match")


class FetchTests:
    """HttpFetcher against the local server and a closed port."""

    def __init__(self, results: Results, server: FixtureServer) -> None:
        # Uses the broken fixture server's /loop route.
        self.r = results
        self.server = server

    def run(self) -> None:
        # Every fetcher test.
        self.r.run("HttpFetcher: a redirect loop stops after 5 hops with an error", "Hidden Failure", self.loop)
        self.r.run("HttpFetcher: a refused connection is status 0 with an error, not an exception", "Hidden Failure", self.refused)
        self.r.run("HttpFetcher: a body over the cap is truncated and flagged", "Edge Case", self.cap)

    def loop(self) -> None:
        result = HttpFetcher(pause=0).get(self.server.origin + "/loop")
        Check.equal(result.error, "too many redirects", "error")
        Check.equal(len(result.redirects), 6, "hops recorded")

    def refused(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = HttpFetcher(pause=0, timeout=3).get("http://127.0.0.1:%d/" % port)
        Check.equal(result.status, 0, "status")
        Check.true(result.error, "error recorded")

    def cap(self) -> None:
        result = HttpFetcher(pause=0, max_bytes=100).get(self.server.origin + "/")
        Check.true(result.truncated and result.bytes == 100, "truncated at 100 bytes (got %d)" % result.bytes)


class IntegrationTests:
    """aeo_audit.py end to end against the fixture sites and the fake CLI."""

    EXPECTED_BROKEN = {
        "ACC-02": "fail", "ACC-05": "fail", "ACC-10": "fail", "REN-03": "fail", "IDX-01": "fail", "IDX-05": "fail",
        "LLM-02": "fail", "LLM-05": "fail", "LLM-06": "fail", "LLM-07": "fail", "MD-03": "fail", "SCH-03": "fail",
        "SCH-06": "warn", "SCH-07": "warn", "SCH-08": "warn", "SCH-11": "warn", "MSG-02": "warn", "ACC-07": "warn", "IDX-04": "warn", "ACC-11": "info",
        "ACC-04": "warn", "ACC-17": "warn", "ACC-18": "warn",
    }

    def __init__(self, results: Results, runner: Runner, fake: FakeCli, base: Path, broken: FixtureServer, clean: FixtureServer) -> None:
        # Keeps the shared fixtures.
        self.r = results
        self.runner = runner
        self.fake = fake
        self.base = base
        self.broken = broken
        self.clean = clean
        self.broken_out = base / "broken"
        self.checks: dict = {}

    def run(self) -> None:
        # Every integration test; the broken gather runs once and feeds several tests.
        r = self.r
        r.run("gather on the broken site writes every file and exits 0", "Hidden Failure", self.gather_broken)
        for check_id, status in self.EXPECTED_BROKEN.items():
            r.run("broken site: %s is %s" % (check_id, status), "Silent Failure", lambda c=check_id, s=status: Check.equal(self.checks.get(c), s, c))
        r.run("broken site: audit.md stays under 40,000 characters and cites evidence", "Edge Case", self.audit_size)
        r.run("broken site: every inputs.json tag exists in evidence.json or the checklist", "Hidden Assumption", self.inputs_tags)
        r.run("clean site: no check fails (no false positives sold as advice)", "Silent Failure", self.gather_clean)
        r.run("gather with nothing listening exits 3 and writes nothing", "Hidden Assumption", self.unreachable)
        r.run("gather with only a question writes a question-only run", "Edge Case", self.question_only)
        r.run("gather with neither --url nor --question exits 2", "Edge Case", self.no_target)
        r.run("gather refuses --max-pages 0 and 61", "Edge Case", self.bad_pages)
        r.run("suggest --dry-run builds the documented argv", "Hidden Assumption", self.dry_run_argv)
        r.run("suggest redacts a key typed into a question before the CLI sees it", "Hidden Failure", self.redacts_question)
        r.run("suggest --paid twice in one folder: the second exits 2 and never calls the CLI", "Hidden Failure", self.paid_twice)
        r.run("suggest --paid propagates the CLI's exit 5 and its error envelope", "Silent Failure", self.exit_five)
        r.run("suggest without the CLI on PATH exits 127", "Hidden Assumption", self.no_cli)
        r.run("report marks an idea with no site evidence as ungrounded", "Silent Failure", self.report_grounding)
        r.run("feedback --reject passes the idea's exact title", "Hidden Assumption", self.feedback_title)
        r.run("broken site: the blocked bots are named in ACC-04, ACC-17, and ACC-18", "Silent Failure", self.broken_bot_details)
        r.run("clean site: ACC-04, ACC-17, and ACC-18 pass with every bot fetched", "Silent Failure", self.clean_bots)
        r.run("suggest --paid adds --project when the learning project exists", "Hidden Assumption", self.project_added)
        r.run("suggest --paid: a failing free dry run stops before the paid run", "Hidden Failure", self.dry_run_gates_paid)
        r.run("feedback with no project creates improve-my-aeo, then records the verdict", "Edge Case", self.feedback_creates_project)

    def gather_broken(self) -> None:
        code, doc, out, err = self.runner.run("gather", "--url", self.broken.origin, "--max-pages", "8", "--out", str(self.broken_out), "--question", "Why is ChatGPT wrong about our price?")
        Check.equal(code, 0, "exit (stderr: %s)" % err[-400:])
        for name in ("audit.md", "checks.json", "evidence.json", "inputs.json", "aeo-rubric.md", "aeo-checklist.md"):
            Check.true((self.broken_out / name).is_file(), name)
        self.checks = {c["id"]: c["status"] for c in json.loads((self.broken_out / "checks.json").read_text(encoding="utf-8"))}
        Check.equal(set(self.checks), set(CHECKS), "one result per check")

    def audit_size(self) -> None:
        text = (self.broken_out / "audit.md").read_text(encoding="utf-8")
        Check.true(len(text) <= 40_000, "audit.md has %d characters" % len(text))
        Check.true("[E1]" in text and "Why is ChatGPT wrong about our price?" in text, "evidence and question present")

    def inputs_tags(self) -> None:
        inputs = json.loads((self.broken_out / "inputs.json").read_text(encoding="utf-8"))
        known = set(json.loads((self.broken_out / "evidence.json").read_text(encoding="utf-8"))) | AeoRun.checklist_ids(CHECKLIST)
        _, problems = AeoRun.validator(known, Redactor()).validate(inputs)
        Check.equal(problems, [], "inputs.json validates")
        Check.true(inputs["mistakes"] and inputs["mistakes"][0].startswith("["), "mistakes pre-filled")

    def gather_clean(self) -> None:
        out = self.base / "clean"
        code, doc, _, err = self.runner.run("gather", "--url", self.clean.origin, "--max-pages", "8", "--out", str(out))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-400:])
        checks = json.loads((out / "checks.json").read_text(encoding="utf-8"))
        failing = ["%s: %s" % (c["id"], c["detail"]) for c in checks if c["status"] == "fail"]
        Check.equal(failing, [], "failing checks on the clean site")

    def unreachable(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        out = self.base / "unreachable"
        code, doc, _, _ = self.runner.run("gather", "--url", "http://127.0.0.1:%d" % port, "--timeout", "2", "--out", str(out))
        Check.equal(code, 3, "exit")
        Check.true(doc and "error" in doc, "error printed")
        Check.true(not (out / "audit.md").exists(), "no files written")

    def question_only(self) -> None:
        out = self.base / "question"
        code, doc, _, _ = self.runner.run("gather", "--question", "Should a bakery add llms.txt?", "--out", str(out))
        Check.equal(code, 0, "exit")
        inputs = json.loads((out / "inputs.json").read_text(encoding="utf-8"))
        Check.true("No site was audited" in inputs["goal"], "question-only goal")
        Check.equal(inputs["question"], ["Should a bakery add llms.txt?"], "question kept verbatim")
        statuses = {c["status"] for c in json.loads((out / "checks.json").read_text(encoding="utf-8"))}
        Check.equal(statuses, {"not_checked"}, "statuses")

    def no_target(self) -> None:
        code, doc, _, _ = self.runner.run("gather")
        Check.equal(code, 2, "exit")

    def bad_pages(self) -> None:
        for value in ("0", "61"):
            code, _, _, _ = self.runner.run("gather", "--url", "http://127.0.0.1:1", "--max-pages", value)
            Check.equal(code, 2, "--max-pages %s" % value)

    def _copy_run(self, name: str) -> Path:
        # A fresh copy of the broken gather output, so paid-run markers never leak between tests.
        target = self.base / name
        shutil.copytree(str(self.broken_out), str(target))
        return target

    def _run_calls(self, before: int) -> list:
        # The `agents suggest run` calls recorded since before, skipping project lookups.
        return [c for c in self.fake.calls()[before:] if c["argv"][2:5] == ["agents", "suggest", "run"]]

    def dry_run_argv(self) -> None:
        out = self._copy_run("dry")
        before = len(self.fake.calls())
        code, doc, _, err = self.runner.run("suggest", "--out", str(out), "--dry-run", "--cli", self.fake.path)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        call = self._run_calls(before)[0]
        argv = call["argv"]
        Check.equal(argv[:6], ["--json", "--no-input", "agents", "suggest", "run", "--goal"], "root options first")
        joined = " ".join(argv)
        for part in ("--files audit.md", "--files aeo-checklist.md", "--instructions aeo-rubric.md", "--max-total-tokens 400000", "--count 8", "--horizon any", "--question", "--dry-run"):
            Check.true(part in joined, "argv has %s" % part)
        Check.equal([argv[i + 1] for i, a in enumerate(argv) if a == "--category"], ["quick_wins", "risk_prevention", "leverage", "strategy", "verification"], "categories")
        Check.equal(Path(call["cwd"]).resolve(), out.resolve(), "runs inside out_dir")

    def redacts_question(self) -> None:
        out = self._copy_run("redact")
        inputs = json.loads((out / "inputs.json").read_text(encoding="utf-8"))
        inputs["question"] = ["Is my key sk-abcdefghijklmnopqrstuvwx leaking into llms.txt?"]
        (out / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
        before = len(self.fake.calls())
        code, _, _, _ = self.runner.run("suggest", "--out", str(out), "--dry-run", "--cli", self.fake.path)
        Check.equal(code, 0, "exit")
        joined = " ".join(self._run_calls(before)[0]["argv"])
        Check.true("sk-abcdefghijklmnopqrstuvwx" not in joined and "[REDACTED]" in joined, "key redacted")

    def paid_twice(self) -> None:
        out = self._copy_run("paid")
        before = len(self.fake.calls())
        first, doc, _, _ = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path)
        Check.equal(first, 0, "first paid run")
        Check.equal(doc["estimated_cost_usd"], round((50000 * 2 + 150000 * 0.2 + 30000 * 10) / 1e6 * 1.02, 2), "cost estimate")
        runs = self._run_calls(before)
        Check.equal(["--dry-run" in c["argv"] for c in runs], [True, False], "a free dry run, then one paid run")
        after_first = len(self.fake.calls())
        second, _, _, _ = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path)
        Check.equal(second, 2, "second paid run refused")
        Check.equal(len(self.fake.calls()), after_first, "second attempt never calls the CLI")

    def exit_five(self) -> None:
        out = self._copy_run("credits")
        code, doc, _, _ = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path, extra_env={"FAKE_CLI_EXIT": "5"})
        Check.equal(code, 5, "exit")
        Check.equal(doc["error"]["code"], "insufficient_credits", "error envelope")
        Check.true(not (out / "paid-run.txt").exists(), "no paid attempt recorded")

    def broken_bot_details(self) -> None:
        checks = {c["id"]: c["detail"] for c in json.loads((self.broken_out / "checks.json").read_text(encoding="utf-8"))}
        Check.true("CCBot" in checks["ACC-04"], "ACC-04 names CCBot: %s" % checks["ACC-04"])
        Check.true("AdsBot-Google (ads)" in checks["ACC-17"], "ACC-17 names the ads bot: %s" % checks["ACC-17"])
        Check.true("facebookexternalhit (preview)" in checks["ACC-18"] and "GPTBot" not in checks["ACC-18"], "ACC-18 names only non-core bots: %s" % checks["ACC-18"])

    def clean_bots(self) -> None:
        out = self.base / "clean"
        statuses = {c["id"]: c["status"] for c in json.loads((out / "checks.json").read_text(encoding="utf-8"))}
        Check.equal((statuses["ACC-04"], statuses["ACC-17"], statuses["ACC-18"]), ("pass", "pass", "pass"), "bot checks")
        audit = (out / "audit.md").read_text(encoding="utf-8")
        Check.true("%d bots fetched the home page; 0 got a different status" % len(BotRegistry.fetchers(BOTS)) in audit, "every bot fetched")

    def project_added(self) -> None:
        out = self._copy_run("project")
        before = len(self.fake.calls())
        code, _, _, _ = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path, extra_env={"FAKE_CLI_PROJECTS": "other,improve-my-aeo"})
        Check.equal(code, 0, "exit")
        for call in self._run_calls(before):
            Check.equal(call["argv"][call["argv"].index("--project") + 1], "improve-my-aeo", "project flag")

    def dry_run_gates_paid(self) -> None:
        out = self._copy_run("dryfail")
        before = len(self.fake.calls())
        code, _, _, err = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path, extra_env={"FAKE_CLI_DRY_EXIT": "2"})
        Check.equal(code, 2, "exit")
        Check.equal(len(self._run_calls(before)), 1, "only the dry run was called")
        Check.true(not (out / "paid-run.txt").exists() and "did not start" in err, "paid run never started")

    def feedback_creates_project(self) -> None:
        out = self._copy_run("feedback-new")
        self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path)
        before = len(self.fake.calls())
        code, _, _, _ = self.runner.run("feedback", "--out", str(out), "--idea", "idea-001", "--accept", "--cli", self.fake.path)
        Check.equal(code, 0, "exit")
        verbs = [c["argv"][3:5] for c in self.fake.calls()[before:]]
        Check.equal(verbs, [["project", "list"], ["project", "create"], ["feedback", "accept"]], "lookup, create, then record")
        create = self.fake.calls()[before + 1]["argv"]
        Check.equal(create[create.index("--key") + 1], "improve-my-aeo", "project key")

    def no_cli(self) -> None:
        out = self._copy_run("nocli")
        code, _, _, _ = self.runner.run("suggest", "--out", str(out), "--dry-run")
        Check.equal(code, 127, "exit")

    def report_grounding(self) -> None:
        out = self._copy_run("report")
        self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path)
        code, doc, _, _ = self.runner.run("report", "--out", str(out))
        Check.equal(code, 0, "exit")
        ideas = {i["id"]: i for i in doc["ideas"]}
        Check.equal(ideas["idea-001"]["grounded"], True, "idea-001 cites E1 and a failing check")
        Check.equal(ideas["idea-002"]["grounded"], False, "idea-002 cites nothing")
        Check.equal(ideas["idea-003"]["unknown_tags"], ["E999"], "unknown tag listed")
        Check.true((out / "report.md").is_file(), "report.md written")

    def feedback_title(self) -> None:
        out = self._copy_run("feedback")
        self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path)
        before = len(self.fake.calls())
        code, _, _, _ = self.runner.run("feedback", "--out", str(out), "--idea", "idea-002", "--reject", "--project", "improve-my-aeo", "--reason", "too vague", "--cli", self.fake.path)
        Check.equal(code, 0, "exit")
        argv = self.fake.calls()[before]["argv"]
        Check.equal(argv[argv.index("--suggestion") + 1], "Write better content", "exact title")
        Check.true(argv[:5] == ["--json", "agents", "suggest", "feedback", "reject"], "command")


def main() -> int:
    # Runs every test against fresh fixtures and returns the exit code.
    results = Results()
    base = Path(tempfile.mkdtemp(prefix="test-improve-my-aeo-"))
    broken, clean = FixtureServer(BrokenSite), FixtureServer(CleanSite)
    try:
        UnitTests(results).run()
        FetchTests(results, broken).run()
        fake = FakeCli(base)
        IntegrationTests(results, Runner(fake), fake, base, broken, clean).run()
    finally:
        broken.close()
        clean.close()
        shutil.rmtree(str(base), ignore_errors=True)
    return results.finish()


if __name__ == "__main__":
    sys.exit(main())
