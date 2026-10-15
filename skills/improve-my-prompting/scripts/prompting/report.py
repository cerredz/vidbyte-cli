"""Maps each idea in result.json back to the user's tagged prompts, sessions, signals, and rubric items."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path

from sessionkit.model import TextTools

TAG_RE = re.compile(r"\[((?:[PS]\d+)(?:[ ,]+[PS]\d+)*)\]")
RUBRIC_ID_RE = re.compile(r"\bR[TCSPREQOAXM]\d{1,2}\b")
RUBRIC_LINE_RE = re.compile(r"^- \*\*(R[TCSPREQOAXM]\d{1,2})\*\* (ALL|OLD|NEW) - (.+?)(?: Source: (.+))?$")
IDEA_TEXT_FIELDS = ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")
QUOTE_CHARS = 300


class ReportError(ValueError):
    """result.json or sessions.json is missing or not what report expects."""


class RubricIndex:
    """Parses the rubric's "- **RT1** ALL - text Source: ..." items (items may wrap over several lines)."""

    def __init__(self, text: str) -> None:
        # Joins wrapped item lines, then indexes every item by its ID.
        self.items: dict = {}
        current = None
        for raw in text.splitlines():
            line = raw.rstrip()
            if line.startswith("- **R"):
                current = line
                self._add(current)
            elif current is not None and line.startswith("  ") and line.strip():
                current = current + " " + line.strip()
                self._add(current)
            else:
                current = None

    def lookup(self, rubric_id: str):
        # {"id", "scope", "text", "source"} for an ID, or None when the rubric has no such item.
        return self.items.get(rubric_id)

    def _add(self, line: str) -> None:
        # Parses one (possibly joined) item line, replacing an earlier partial parse of it.
        match = RUBRIC_LINE_RE.match(line)
        if match:
            self.items[match.group(1)] = {"id": match.group(1), "scope": match.group(2), "text": match.group(3).strip(), "source": (match.group(4) or "").strip().rstrip(".")}


class IdeaReporter:
    """Builds report.md and the JSON report from result.json, sessions.json, and the rubric copy."""

    def __init__(self, out_dir: Path) -> None:
        # Reads nothing until build().
        self.out_dir = Path(out_dir)

    def build(self) -> dict:
        # Returns the JSON report and writes report.md; raises ReportError for missing or wrong inputs.
        result, index, rubric = self._load()
        ideas = (result.get("data") or {}).get("ideas") or []
        prompts = {p["tag"]: p for p in index.get("prompts", []) if isinstance(p, dict) and p.get("tag")}
        sessions = {s["tag"]: s for s in index.get("sessions", []) if isinstance(s, dict) and s.get("tag")}
        mapped = [self._map_idea(idea, prompts, sessions, rubric) for idea in ideas if isinstance(idea, dict)]
        report = OrderedDict([
            ("report_file", str(self.out_dir / "report.md")),
            ("status", (result.get("data") or {}).get("status")),
            ("ideas", mapped),
            ("ungrounded", [m["id"] for m in mapped if not m["grounded"]]),
        ])
        TextTools.write_lf(self.out_dir / "report.md", self._markdown(mapped, result))
        return report

    def _load(self):
        # Loads result.json (must be suggestions.result), sessions.json, and the rubric copy.
        try:
            with open(str(self.out_dir / "result.json"), encoding="utf-8") as handle:
                result = json.load(handle)
        except FileNotFoundError:
            raise ReportError("result.json not found in %s. Run `suggest --paid` first." % self.out_dir)
        except ValueError as error:
            raise ReportError("result.json is not valid JSON (%s)." % error)
        if not isinstance(result, dict) or result.get("kind") != "suggestions.result":
            raise ReportError("result.json is not a suggestions.result document.")
        index = TextTools.read_json_file(self.out_dir / "sessions.json")
        if not isinstance(index, dict):
            raise ReportError("sessions.json not found or invalid in %s. Run `gather` into this folder first." % self.out_dir)
        rubric_path = self.out_dir / "prompting-rubric.md"
        rubric = RubricIndex(rubric_path.read_text(encoding="utf-8") if rubric_path.is_file() else "")
        return result, index, rubric

    def _map_idea(self, idea: dict, prompts: dict, sessions: dict, rubric: RubricIndex) -> dict:
        # Finds the idea's tags and rubric IDs and resolves them against the index and rubric.
        text = self._idea_text(idea)
        tags = list(OrderedDict.fromkeys(t for group in TAG_RE.findall(text) for t in re.split(r"[ ,]+", group) if t))
        rubric_ids = list(OrderedDict.fromkeys(RUBRIC_ID_RE.findall(text)))
        cited_prompts = [self._prompt_entry(prompts[t], sessions) for t in tags if t in prompts]
        cited_sessions = [self._session_entry(sessions[t]) for t in tags if t.startswith("S") and t in sessions]
        signals = sorted({s for p in cited_prompts for s in p["signals"]})
        return OrderedDict([
            ("id", idea.get("id")), ("rank", idea.get("rank")), ("title", idea.get("title")),
            ("horizon", idea.get("horizon")), ("primary_category", idea.get("primary_category")),
            ("prompts", cited_prompts), ("sessions", cited_sessions), ("signals", signals),
            ("rubric", [rubric.lookup(r) for r in rubric_ids if rubric.lookup(r)]),
            ("grounded", bool(cited_prompts or cited_sessions)),
            ("unknown_tags", [t for t in tags if t not in prompts and t not in sessions]),
            ("unknown_rubric_ids", [r for r in rubric_ids if not rubric.lookup(r)]),
        ])

    @staticmethod
    def _idea_text(idea: dict) -> str:
        # Concatenates the idea's text fields and its evidence contents.
        parts = [str(idea.get(f) or "") for f in IDEA_TEXT_FIELDS]
        handoff = idea.get("handoff") if isinstance(idea.get("handoff"), dict) else {}
        for evidence in handoff.get("evidence") or []:
            if isinstance(evidence, dict):
                parts.append(str(evidence.get("content") or ""))
        return "\n".join(parts)

    @staticmethod
    def _prompt_entry(prompt: dict, sessions: dict) -> dict:
        # A cited prompt with its excerpt, session, harness, project, time, and signals.
        session = sessions.get(prompt.get("session"), {})
        return OrderedDict([
            ("tag", prompt["tag"]), ("session", prompt.get("session")), ("harness", session.get("harness")), ("project", session.get("project")),
            ("ts", prompt.get("ts")), ("text", TextTools.one_line(prompt.get("text", ""), QUOTE_CHARS)), ("signals", prompt.get("signals") or []),
        ])

    @staticmethod
    def _session_entry(session: dict) -> dict:
        # A cited session with its harness, project, title, and prompt count.
        return OrderedDict((k, session.get(k)) for k in ("tag", "harness", "project", "title", "last", "prompts", "end_state"))

    def _markdown(self, mapped: list, result: dict) -> str:
        # report.md: each idea with its habit, cited prompts, rubric items, fix, and check.
        ideas = {i.get("id"): i for i in (result.get("data") or {}).get("ideas") or [] if isinstance(i, dict)}
        lines = ["# Prompting critique", ""]
        if not mapped:
            lines.append("No ideas in result.json (status %s)." % (result.get("data") or {}).get("status"))
        for entry in mapped:
            idea = ideas.get(entry["id"], {})
            lines += ["## %s. %s" % (entry.get("rank") or "?", entry.get("title") or "(untitled)"), ""]
            if not entry["grounded"]:
                lines += ["> Not grounded: this idea cites none of your tagged prompts or sessions. Treat it as a general tip.", ""]
            lines += [str(idea.get("summary") or ""), ""]
            for prompt in entry["prompts"]:
                lines.append('- [%s] %s, %s: "%s"' % (prompt["tag"], prompt.get("harness"), prompt.get("project") or "?", prompt["text"]))
            for session in entry["sessions"]:
                lines.append("- [%s] %s, %s: %s (%s prompts)" % (session["tag"], session.get("harness"), session.get("project") or "?", session.get("title") or "untitled", session.get("prompts")))
            for item in entry["rubric"]:
                lines.append("- Rubric %s (%s): %s%s" % (item["id"], item["scope"], item["text"], (" Source: " + item["source"]) if item["source"] else ""))
            if idea.get("first_action"):
                lines += ["", "**Fix:** " + str(idea["first_action"])]
            if idea.get("completion_criteria"):
                lines += ["", "**Check:** " + str(idea["completion_criteria"])]
            if entry["unknown_tags"]:
                lines += ["", "Unknown tags (not in sessions.json): " + ", ".join(entry["unknown_tags"])]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
