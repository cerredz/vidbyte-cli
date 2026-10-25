"""Writes the gather output: audit.md (sent to the model), checks.json, evidence.json, and the pre-filled inputs.json."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from .checks import CHECKS, FAIL, INFO, NOT_CHECKED, PASS, STATUS_ORDER, WARN, CheckResult
from .evidence import EvidenceIndex
from .html import JsonLd
from .runner import TextTools
from .suggest import MAX_GOAL_CHARS, MAX_VALUE_CHARS

MAX_DIGEST_CHARS = 40_000
NOT_MECHANICAL = (
    "Not checked by the script: off-site entity and reputation signals (Wikipedia, Wikidata, reviews, Reddit, press, listicles), "
    "how often answer engines cite the site for real prompts, content quality beyond counts, and anything that only appears after JavaScript runs."
)


class DigestWriter:
    """Turns facts and check results into the files the rest of the skill reads."""

    def __init__(self, out_dir: Path, facts: dict, results: list, evidence: EvidenceIndex, questions: list) -> None:
        # facts is None for a question-only run.
        self.out_dir = Path(out_dir)
        self.facts = facts
        self.results = sorted(results, key=lambda r: (STATUS_ORDER[r.status], -r.severity, r.id))
        self.ev = evidence
        self.questions = questions

    def write(self) -> dict:
        # Writes all four files and returns {name: path} plus the digest size.
        digest = self.digest()
        TextTools.write_lf(self.out_dir / "audit.md", digest)
        TextTools.write_lf(self.out_dir / "checks.json", json.dumps([r.to_json() for r in self.results], indent=1, ensure_ascii=False) + "\n")
        TextTools.write_lf(self.out_dir / "evidence.json", json.dumps(self.ev.to_json(), indent=1, ensure_ascii=False) + "\n")
        TextTools.write_lf(self.out_dir / "inputs.json", json.dumps(self.inputs(), indent=2, ensure_ascii=False) + "\n")
        return {"audit_chars": len(digest)}

    def digest(self) -> str:
        # audit.md: header, totals, failing and warning checks, passes, other checks, site facts, then the evidence index, trimmed to MAX_DIGEST_CHARS.
        head = self._header()
        sections = [self._problems(), self._passes(), self._others(), self._site_facts(), self._evidence_section()]
        out = head
        for section in sections:
            if len(out) + len(section) > MAX_DIGEST_CHARS:
                room = MAX_DIGEST_CHARS - len(out) - 80
                if room > 500:
                    out += section[:room] + "\n\n[trimmed to keep audit.md under %d characters]\n" % MAX_DIGEST_CHARS
                break
            out += section
        return out

    def _header(self) -> str:
        # Who, what, when, totals, and how tags work.
        if not self.facts:
            lines = ["# AEO questions (no site audited)", "", "No site was audited, so there is no [E#] evidence. Answer from aeo-checklist.md and cite its item ids.", ""]
        else:
            counts = {s: sum(1 for r in self.results if r.status == s) for s in (FAIL, WARN, PASS, INFO, NOT_CHECKED)}
            pages = self.facts.get("pages", [])
            lines = [
                "# AEO audit: %s" % self.facts.get("host"),
                "",
                "Audited %s from the site owner's machine with raw HTTP requests (no JavaScript). %d pages sampled; the sitemap lists %d URLs. Platform markers: %s." % (
                    self.facts.get("audited_at"), len(pages), len(self.facts.get("sitemap_urls", [])), ", ".join(sorted({f for p in pages for f in p.get("frameworks", [])})) or "none"),
                "Checks: %d fail, %d warn, %d pass, %d info, %d not checked." % (counts[FAIL], counts[WARN], counts[PASS], counts[INFO], counts[NOT_CHECKED]),
                "",
                "Tags: [E#] is one observed fact (listed under each check and in the evidence index). [ACC-01] and similar ids are items in aeo-checklist.md.",
                NOT_MECHANICAL,
                "",
            ]
        if self.questions:
            lines += ["## The user's questions", ""] + ["- " + q for q in self.questions] + [""]
        return "\n".join(lines) + "\n"

    def _problems(self) -> str:
        # Every fail and warn with its detail and evidence lines.
        rows = [r for r in self.results if r.status in (FAIL, WARN)]
        if not rows:
            return "## Failing and warning checks\n\nNone.\n\n"
        out = ["## Failing and warning checks", ""]
        for r in rows:
            out.append("### [%s] %s - %s (severity %d)" % (r.id, r.title, r.status.upper(), r.severity))
            out.append(r.detail)
            out += ["- [%s] %s%s" % (t, self.ev.items[t]["text"], (" (" + self.ev.items[t]["url"] + ")") if self.ev.items[t]["url"] else "") for t in r.evidence if t in self.ev.items]
            out.append("")
        return "\n".join(out) + "\n"

    def _passes(self) -> str:
        # One line per passing check.
        rows = [r for r in self.results if r.status == PASS]
        return "## Passing checks\n\n" + "".join("- [%s] %s: %s%s\n" % (r.id, r.title, r.detail, (" (" + " ".join(r.evidence[:3]) + ")") if r.evidence else "") for r in rows) + "\n"

    def _others(self) -> str:
        # Info and not-checked results.
        rows = [r for r in self.results if r.status in (INFO, NOT_CHECKED)]
        return "## Info and not checked\n\n" + "".join("- [%s] %s - %s: %s\n" % (r.id, r.title, r.status, r.detail) for r in rows) + "\n"

    def _site_facts(self) -> str:
        # Tables of the raw facts: robots tokens, user-agent probes, llms.txt head, pages, brand strings.
        if not self.facts:
            return ""
        f = self.facts
        out = ["## Site facts", "", "### robots.txt verdicts for AI tokens (home page)", "", "| Token | Purpose | Named in robots.txt | Home allowed |", "|---|---|---|---|"]
        out += ["| %s | %s | %s | %s |" % (t["agent"], t["purpose"], "yes" if t["named"] else "no", "yes" if t["home_allowed"] else "NO") for t in f.get("robots", {}).get("tokens", [])]
        out += ["", "### Home page fetched as each user agent", "", "| Agent | Status | Bytes | Words |", "|---|---|---|---|"]
        out += ["| %s | %s | %s | %s |" % (r["agent"], r["status"], r["bytes"], r["words"]) for r in f.get("ua_probes", [])]
        llms = f.get("llms", {})
        out += ["", "### llms.txt (first 2,500 characters)", ""]
        out += ["```text", llms["text"][:2500], "```"] if llms.get("text") else ["Not found (GET /llms.txt -> %s)." % llms.get("status")]
        out += ["", "### Sampled pages", "", "| Page | Status | Words | H1 | Title | Schema types | MD mirror |", "|---|---|---|---|---|---|---|"]
        for p in f.get("pages", []):
            types = sorted({t for n in p.get("jsonld", []) for t in JsonLd.types(n)})
            path = urllib.parse.urlsplit(p["url"]).path or "/"
            mirror = p.get("md_mirror") and urllib.parse.urlsplit(p["md_mirror"]["url"]).path
            out.append("| %s | %s | %s | %s | %s | %s | %s |" % (path, p.get("status"), p.get("words"), self._cell((p.get("h1") or [""])[0]), self._cell(p.get("title") or ""), ", ".join(types) or "-", mirror or ("-" if "md_mirror" in p else "not probed")))
        out += ["", "### Meta descriptions", ""]
        out += ["- %s: %s" % (urllib.parse.urlsplit(p["url"]).path or "/", self._cell(p.get("description") or "(none)", 220)) for p in f.get("pages", []) if p.get("status") == 200]
        out += ["", "### Claims found in page text (prices, free claims, status words)", ""]
        for p in f.get("pages", []):
            c = p.get("claims") or {}
            if any(c.values()):
                out.append("- %s: prices %s; free %s; status %s" % (urllib.parse.urlsplit(p["url"]).path or "/", ", ".join(c.get("prices", [])[:8]) or "-", " / ".join(c.get("free", [])[:2]) or "-", " / ".join(c.get("status", [])[:2]) or "-"))
        out += ["", "### Opening text of each page (raw HTML, first 600 characters)", ""]
        out += ["- %s: %s" % (urllib.parse.urlsplit(p["url"]).path or "/", self._cell(p.get("text_sample", ""), 600)) for p in f.get("pages", []) if p.get("text_sample")]
        return "\n".join(out) + "\n\n"

    def _evidence_section(self) -> str:
        # Every evidence item, so any tag an idea cites can be found.
        return "## Evidence index\n\n" + "".join("- [%s] %s%s\n" % (t, e["text"], (" (" + e["url"] + ")") if e["url"] else "") for t, e in self.ev.items.items())

    @staticmethod
    def _cell(text: str, limit: int = 80) -> str:
        # One table-safe line.
        return TextTools.one_line(str(text).replace("|", "/"), limit)

    def inputs(self) -> dict:
        # The typed inputs, pre-filled from the checks; the host agent adds only the user's own words.
        host = self.facts.get("host") if self.facts else None
        problems = [r for r in self.results if r.status in (FAIL, WARN)]
        problems.sort(key=lambda r: (-r.severity, STATUS_ORDER[r.status], r.id))
        passes = sorted((r for r in self.results if r.status == PASS), key=lambda r: (-r.severity, r.id))
        return {
            "goal": self._goal(host)[:MAX_GOAL_CHARS],
            "project": None,
            "question": list(self.questions),
            "mistakes": [self._value(r) for r in problems[:8]],
            "outcomes": [self._value(r) for r in passes[:5]],
            "context": self._context(host),
            "capability": [],
            "success": [],
            "constraint": [],
            "avoid": [],
        }

    def _goal(self, host) -> str:
        # The goal for audit, audit-plus-questions, and question-only runs.
        if not host:
            return ("Answer my questions about answer-engine optimization (AEO): how to get a site read, trusted, and cited by ChatGPT, Claude, Perplexity, "
                    "Google AI Overviews and AI Mode, and Copilot. No site was audited; use aeo-checklist.md as the reference, cite its item ids, and give exact changes and how to verify each.")
        lead = "Answer my questions about the AEO of %s, then find" % host if self.questions else "Find"
        return ("%s the changes that most improve how answer engines (ChatGPT, Claude, Perplexity, Google AI Overviews and AI Mode, Copilot) and AI agents read, trust, and cite %s. "
                "audit.md is a free mechanical audit of the site; aeo-checklist.md is the reference. Each idea is one fix: cite [E#] evidence and a checklist id, give the exact change, "
                "and a check the audit can re-run." % (lead, host))

    @staticmethod
    def _value(r: CheckResult) -> str:
        # "[ID E1 E2] Title - STATUS: detail", trimmed to the value limit.
        tags = " ".join([r.id] + r.evidence[:3])
        return TextTools.one_line("[%s] %s - %s: %s" % (tags, r.title, r.status.upper(), r.detail), MAX_VALUE_CHARS)

    def _context(self, host) -> list:
        # Background the critic needs: what was audited, the platform, and what was not checked.
        if not host:
            return ["[all] No site was audited. The user asked general AEO questions; there is no [E#] evidence."]
        pages = self.facts.get("pages", [])
        frameworks = sorted({f for p in pages for f in p.get("frameworks", [])})
        counts = {s: sum(1 for r in self.results if r.status == s) for s in (FAIL, WARN, PASS)}
        return [
            TextTools.one_line("[all] %s audited %s: %d pages sampled, %d URLs in the sitemap, platform markers %s; %d checks fail, %d warn, %d pass." % (
                self.facts.get("origin"), (self.facts.get("audited_at") or "")[:10], len(pages), len(self.facts.get("sitemap_urls", [])), ", ".join(frameworks) or "none", counts[FAIL], counts[WARN], counts[PASS]), MAX_VALUE_CHARS),
            TextTools.one_line("[all] " + NOT_MECHANICAL, MAX_VALUE_CHARS),
        ]

    @staticmethod
    def checks_catalog() -> dict:
        # id -> title for every mechanical check, used by tests and the report.
        return {cid: title for cid, (_sev, title) in CHECKS.items()}
