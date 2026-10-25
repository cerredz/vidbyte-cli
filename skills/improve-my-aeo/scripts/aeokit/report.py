"""Ties each idea in result.json to the audit evidence and checklist items it cites, and flags ungrounded ideas."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .runner import TextTools
from .suggest import CHECKLIST_ID_RE, AeoRun

TAG_RE = re.compile(r"\bE\d+\b")
ID_RE = re.compile(r"\b[A-Z]{2,3}-\d{2}\b")
IDEA_FIELDS = ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")


class ReportError(Exception):
    """A missing or unreadable input file; the message says what to run."""


class IdeaReporter:
    """Reads result.json, evidence.json, checks.json, and the checklist, and writes report.md."""

    def __init__(self, out_dir: Path) -> None:
        # out_dir is the folder gather wrote and suggest ran in.
        self.out_dir = Path(out_dir)

    def build(self) -> dict:
        # The report as data; also writes report.md.
        result = self._load("result.json")
        evidence = self._load("evidence.json")
        checks = {c["id"]: c for c in self._load("checks.json")}
        checklist = self._checklist_titles()
        data = result.get("data", {}) if isinstance(result, dict) else {}
        site = any(c["status"] != "not_checked" for c in checks.values())
        ideas = [self._idea(i, evidence, checks, checklist, site) for i in data.get("ideas") or [] if isinstance(i, dict)]
        report = {"status": data.get("status"), "stop_reason": data.get("stop_reason"), "site_audited": site, "ideas": ideas,
                  "ungrounded": [i["id"] for i in ideas if not i["grounded"]], "report_file": str(self.out_dir / "report.md")}
        TextTools.write_lf(self.out_dir / "report.md", self._markdown(report))
        return report

    def _load(self, name: str):
        # Loads one JSON file from out_dir or raises ReportError with the fix.
        path = self.out_dir / name
        try:
            with open(str(path), encoding="utf-8-sig") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise ReportError("%s not found in %s. Run `gather`, then `suggest --paid`, before `report`." % (name, self.out_dir))
        except ValueError as error:
            raise ReportError("%s is not valid JSON: %s" % (name, error))

    def _checklist_titles(self) -> dict:
        # id -> title from aeo-checklist.md in out_dir.
        path = self.out_dir / "aeo-checklist.md"
        titles = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                match = CHECKLIST_ID_RE.match(line)
                if match:
                    titles[match.group(1)] = line.split(match.group(1), 1)[1].strip(" -—:")
        return titles

    def _idea(self, idea: dict, evidence: dict, checks: dict, checklist: dict, site: bool) -> dict:
        # One idea's citations: evidence lines, checklist items (with their audit status), and whether it is grounded.
        text = " ".join(str(idea.get(f) or "") for f in IDEA_FIELDS)
        handoff = idea.get("handoff") or {}
        text += " " + " ".join(str(e.get("content") or "") for e in handoff.get("evidence") or [] if isinstance(e, dict))
        tags = sorted(set(TAG_RE.findall(text)), key=lambda t: int(t[1:]))
        ids = sorted(set(ID_RE.findall(text)))
        cited = [{"tag": t, "text": evidence[t]["text"], "url": evidence[t]["url"]} for t in tags if t in evidence]
        items = [{"id": i, "title": checklist.get(i) or (checks.get(i) or {}).get("title"), "audit_status": (checks.get(i) or {}).get("status")} for i in ids if i in checklist or i in checks]
        failing = any(it["audit_status"] in ("fail", "warn") for it in items)
        grounded = bool(cited or failing) if site else bool(items)
        return {"id": idea.get("id"), "rank": idea.get("rank"), "title": idea.get("title"), "evidence": cited, "checklist": items, "grounded": grounded,
                "unknown_tags": [t for t in tags if t not in evidence], "unknown_ids": [i for i in ids if i not in checklist and i not in checks]}

    @staticmethod
    def _markdown(report: dict) -> str:
        # report.md: one section per idea with its citations.
        out = ["# AEO fixes: what each idea cites", "", "Status: %s (%s)." % (report["status"], report["stop_reason"]), ""]
        for idea in report["ideas"]:
            out.append("## %s. %s%s" % (idea.get("rank") or "-", idea.get("title"), "" if idea["grounded"] else "  (not tied to this site's evidence)"))
            out += ["- Checklist: [%s] %s (audit: %s)" % (c["id"], c["title"], c["audit_status"] or "not a script check") for c in idea["checklist"]]
            out += ["- Evidence [%s]: %s%s" % (e["tag"], e["text"], (" (" + e["url"] + ")") if e["url"] else "") for e in idea["evidence"]]
            if idea["unknown_tags"] or idea["unknown_ids"]:
                out.append("- Unknown citations: %s" % ", ".join(idea["unknown_tags"] + idea["unknown_ids"]))
            out.append("")
        return "\n".join(out) + "\n"


__all__ = ["IdeaReporter", "ReportError", "AeoRun"]
