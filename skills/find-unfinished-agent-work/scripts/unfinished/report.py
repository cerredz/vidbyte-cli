"""Turns the run's ideas into one Markdown checklist, joined with the local index after the run.

Each idea's [S#] and [P#] tags are looked up in sessions.json for the project, the last activity, the
local status, and the resume command, none of which the model ever saw. Ideas with no valid tag are
flagged, and every open session no idea cites is added, so the checklist covers all the open work the
local check found.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import OrderedDict
from pathlib import Path

from sessionkit.model import Clock, TextTools

from .suggest import CHECKLIST_FILE, INDEX_FILE, RESULT_FILE

TAG_GROUP_RE = re.compile(r"\[((?:[SP]\d+)(?:[ ,;]+[SP]\d+)*)\]")
TEXT_FIELDS = ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")
STATUS_RANK = {"still open": 0, "unknown": 1, "probably done": 2, "folded": 3}
FLOOR = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
UNTRACED = "Not traced to a session"
LOCAL_ONLY_RESULT = {"kind": "suggestions.result", "data": {"ideas": [], "status": "local_only", "stop_reason": None}}


class ReportError(ValueError):
    """A missing or malformed input file; the command prints it and exits 2."""


class SessionIndex:
    """sessions.json, indexed by session tag and prompt tag."""

    def __init__(self, data: dict) -> None:
        # Builds the tag lookups from a loaded sessions.json document.
        self.data = data
        self.items = [e for e in data.get("items") or [] if isinstance(e, dict)]
        self.by_tag = {e["tag"]: e for e in self.items if e.get("tag")}
        self.prompt_session = {p.get("tag"): p.get("session") for p in data.get("prompts") or [] if isinstance(p, dict)}

    def resolve(self, tags: list) -> tuple:
        # (session tags that exist, in first-seen order; tags that do not exist).
        found, unknown = [], []
        for tag in tags:
            session_tag = self.prompt_session.get(tag) if tag.startswith("P") else tag
            if session_tag and session_tag in self.by_tag:
                if session_tag not in found:
                    found.append(session_tag)
            elif tag not in unknown:
                unknown.append(tag)
        return found, unknown


class ChecklistReporter:
    """Builds checklist.md and the JSON summary from result.json and sessions.json."""

    def __init__(self, out_dir: Path, shell: str = None, local_only: bool = False) -> None:
        # shell is "bash" or "powershell" (None: PowerShell on Windows, bash elsewhere); local_only skips result.json.
        self.out_dir = Path(out_dir)
        self.shell = shell or ("powershell" if os.name == "nt" else "bash")
        self.local_only = local_only

    def build(self) -> dict:
        # Maps every idea to its sessions, adds the open sessions no idea cites, writes checklist.md, returns JSON.
        result = LOCAL_ONLY_RESULT if self.local_only else self._load(RESULT_FILE)
        if not isinstance(result, dict) or result.get("kind") != "suggestions.result":
            raise ReportError("%s is not a suggestions.result document; run `suggest --paid` first." % RESULT_FILE)
        index = SessionIndex(self._load(INDEX_FILE))
        ideas = [i for i in (result.get("data") or {}).get("ideas") or [] if isinstance(i, dict)]
        entries = [self._map_idea(idea, index) for idea in ideas]
        cited = {tag for e in entries for tag in e["sessions"]}
        added = self._uncited_open(index, cited)
        markdown = self._render(entries, added, index, result)
        TextTools.write_lf(self.out_dir / CHECKLIST_FILE, markdown)
        return self._summary(entries, added, result)

    def _load(self, name: str):
        # Loads one JSON file from the output folder, or raises ReportError naming the fix.
        path = self.out_dir / name
        try:
            with open(str(path), encoding="utf-8-sig") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise ReportError("%s not found in %s. Run gather and then suggest --paid in this folder." % (name, self.out_dir)) from None
        except ValueError as error:
            raise ReportError("%s is not valid JSON: %s" % (name, error)) from None

    def _map_idea(self, idea: dict, index: SessionIndex) -> dict:
        # One checklist entry: the idea's own fields plus its sessions, project, last activity, and flags.
        own = self._tags(" ".join(self._text(idea.get(f)) for f in TEXT_FIELDS) + " " + " ".join(self._text(a) for a in idea.get("suggested_actions") or []))
        evidence = self._tags(" ".join(self._text(e.get("content")) for e in ((idea.get("handoff") or {}).get("evidence") or []) if isinstance(e, dict)))
        sessions, unknown = index.resolve(own)
        from_evidence = False
        if not sessions:
            sessions, unknown_e = index.resolve(evidence)
            unknown += [t for t in unknown_e if t not in unknown]
            from_evidence = bool(sessions)
        records = [index.by_tag[t] for t in sessions]
        records.sort(key=lambda e: self._stamp(e.get("last")), reverse=True)
        flags = []
        if not records:
            flags.append("Not traced to any session in the digest; check before acting on it.")
        elif from_evidence:
            flags.append("The item's text names no session; its sessions come from the evidence it cited.")
        statuses = sorted({e.get("status") for e in records}, key=lambda s: STATUS_RANK.get(s, 9))
        if records and statuses and statuses[0] == "probably done":
            flags.append("The local check says this looks finished (%s); verify before working on it." % TextTools.one_line(records[0].get("reason"), 160))
        return {
            "idea_id": idea.get("id"),
            "rank": idea.get("rank"),
            "title": TextTools.one_line(idea.get("title"), 200),
            "summary": TextTools.one_line(idea.get("summary"), 600),
            "first_action": TextTools.one_line(idea.get("first_action"), 400),
            "completion_criteria": TextTools.one_line(idea.get("completion_criteria"), 300),
            "horizon": idea.get("horizon"),
            "readiness": idea.get("readiness"),
            "sessions": [e["tag"] for e in records],
            "records": records,
            "project": records[0].get("project") if records else UNTRACED,
            "last": records[0].get("last") if records else None,
            "local_status": statuses[0] if statuses else None,
            "grounded": bool(records),
            "unknown_tags": unknown,
            "flags": flags,
        }

    def _uncited_open(self, index: SessionIndex, cited: set) -> list:
        # Open sessions (still open first, then unknown) that no idea cites, newest first.
        found = [e for e in index.items if e.get("status") in ("still open", "unknown") and e.get("tag") not in cited]
        found.sort(key=lambda e: (STATUS_RANK.get(e.get("status"), 9), -self._stamp(e.get("last")).timestamp()))
        return found

    def _render(self, entries: list, added: list, index: SessionIndex, result: dict) -> str:
        # One Markdown checklist grouped by project, projects and items newest first; untraced items last.
        groups: "OrderedDict[str, list]" = OrderedDict()
        rows = [("idea", e, self._stamp(e["last"])) for e in entries if e["grounded"]]
        rows += [("local", e, self._stamp(e.get("last"))) for e in added]
        rows.sort(key=lambda r: r[2], reverse=True)
        for kind, entry, _stamp in rows:
            project = entry["project"] if kind == "idea" else entry.get("project") or "(no folder)"
            groups.setdefault(project, []).append((kind, entry))
        untraced = [e for e in entries if not e["grounded"]]
        lines = self._intro(entries, added, index, result)
        for project, members in groups.items():
            lines.append("## %s" % project)
            lines.append("")
            for kind, entry in members:
                lines.extend(self._idea_lines(entry, index) if kind == "idea" else self._local_lines(entry))
            lines.append("")
        if untraced:
            lines.append("## %s" % UNTRACED)
            lines.append("")
            for entry in untraced:
                lines.extend(self._idea_lines(entry, index))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _intro(self, entries: list, added: list, index: SessionIndex, result: dict) -> list:
        # The checklist title and one paragraph on what it covers.
        counts = index.data.get("counts") or {}
        window = index.data.get("window") or {}
        since = window.get("since")
        span = ("since %s" % Clock.local_stamp(Clock.parse_ts(since))) if since else "with no time limit"
        if self.local_only:
            found = "%d open session%s found by the local check alone, without a Suggestion Agent run" % (len(added), "" if len(added) == 1 else "s")
        else:
            found = "%d item%s from the Vidbyte Suggestion Agent; %d more open session%s found by the local check and not listed by it" % (
                len(entries), "" if len(entries) == 1 else "s", len(added), "" if len(added) == 1 else "s")
        lines = [
            "# Unfinished agent work",
            "",
            "Built %s from your %s most recent coding-agent sessions, %s. %s. Run each resume command from a terminal; "
            "it opens the session in the folder it ran in." % (Clock.local_stamp(Clock.now()), counts.get("sessions", "?"), span, found),
            "",
        ]
        if not entries and not self.local_only:
            data = result.get("data") or {}
            lines += ["The run returned no items (status `%s`, stop reason `%s`)." % (data.get("status"), data.get("stop_reason")), ""]
        return lines

    def _idea_lines(self, entry: dict, index: SessionIndex) -> list:
        # The checklist lines for one idea.
        lines = ["- [ ] **%s**%s" % (entry["title"], " (%s)" % entry["idea_id"] if entry["idea_id"] else "")]
        if entry["records"]:
            sessions = ", ".join("[%s] %s (%s)" % (r["tag"], self._harness(r.get("harness")), Clock.local_stamp(Clock.parse_ts(r.get("last")))) for r in entry["records"])
            lines.append("  - Project: %s · Sessions: %s · Last touched: %s" % (entry["project"], sessions, Clock.local_stamp(Clock.parse_ts(entry["last"]))))
            head = entry["records"][0]
            lines.append("  - Local check: %s — %s" % (head.get("status"), TextTools.one_line(head.get("reason"), 260)))
        if entry["summary"]:
            lines.append("  - Why unfinished: %s" % entry["summary"])
        if entry["first_action"]:
            lines.append("  - First step: %s" % entry["first_action"])
        if entry["completion_criteria"]:
            lines.append("  - Done when: %s" % entry["completion_criteria"])
        for record in entry["records"]:
            lines.extend(self._resume_lines(record))
        for flag in entry["flags"]:
            lines.append("  - Check: %s" % flag)
        if entry["unknown_tags"]:
            lines.append("  - Check: cites %s, which the digest does not have." % ", ".join(entry["unknown_tags"]))
        return lines

    def _local_lines(self, record: dict) -> list:
        # The checklist lines for an open session that no idea cites.
        status = record.get("status")
        verb = "Finish or close" if status == "still open" else "Check whether anything is left in"
        tag = "[%s] " % record["tag"] if record.get("tag") else ""
        source = "found by the local check" if self.local_only else "found by the local check; the Suggestion Agent did not list it"
        lines = [
            '- [ ] **%s the session "%s"** (%s)' % (verb, TextTools.one_line(record.get("title"), 120), source),
            "  - Project: %s · Session: %s%s · Last touched: %s" % (record.get("project"), tag, self._harness(record.get("harness")), Clock.local_stamp(Clock.parse_ts(record.get("last")))),
            "  - Local check: %s — %s" % (status, TextTools.one_line(record.get("reason"), 260)),
        ]
        lines.extend(self._left_lines(record.get("left") or {}))
        lines.extend(self._resume_lines(record))
        return lines

    @staticmethod
    def _left_lines(left: dict) -> list:
        # "Left to do" from the session's open todos and remaining-work lines, and its unanswered question.
        lines = []
        todos = [TextTools.one_line(t, 160) for t in left.get("todos") or [] if t]
        if todos:
            lines.append("  - Open todos: %s" % "; ".join(todos))
        remaining = [TextTools.one_line(r, 240) for r in left.get("remaining") or [] if r]
        if remaining:
            lines.append("  - The agent said: %s" % " ".join('"%s"' % r for r in remaining))
        if left.get("question"):
            lines.append('  - Unanswered question: "%s"' % TextTools.one_line(left["question"], 240))
        return lines

    def _resume_lines(self, record: dict) -> list:
        # "Resume [S4]: `command`" in the chosen shell, plus the note when there is one.
        resume = record.get("resume") or {}
        command = resume.get(self.shell) or resume.get("command")
        if not command:
            return []
        label = record.get("tag") or record.get("session_id", "")[:8]
        lines = ["  - Resume [%s]: `%s`" % (label, command)]
        if resume.get("note") and not resume.get("cwd"):
            lines.append("  - Note: %s" % resume["note"])
        return lines

    def _summary(self, entries: list, added: list, result: dict) -> dict:
        # The JSON the report command prints.
        data = result.get("data") or {}
        return {
            "checklist": str(self.out_dir / CHECKLIST_FILE),
            "shell": self.shell,
            "status": data.get("status"),
            "stop_reason": data.get("stop_reason"),
            "items": [
                {k: e[k] for k in ("idea_id", "rank", "title", "project", "sessions", "last", "local_status", "horizon", "readiness", "grounded", "unknown_tags", "flags")}
                for e in entries
            ],
            "added_from_local_check": [
                {"tag": e.get("tag"), "status": e.get("status"), "project": e.get("project"), "title": e.get("title"), "last": e.get("last")} for e in added
            ],
            "ungrounded": [e["idea_id"] for e in entries if not e["grounded"]],
            "counts": {"ideas": len(entries), "grounded": sum(1 for e in entries if e["grounded"]), "added_from_local_check": len(added)},
        }

    @staticmethod
    def _tags(text: str) -> list:
        # Every S#/P# tag inside square brackets, in first-seen order.
        found = []
        for group in TAG_GROUP_RE.findall(text or ""):
            for tag in re.split(r"[ ,;]+", group):
                if tag and tag not in found:
                    found.append(tag)
        return found

    @staticmethod
    def _text(value: object) -> str:
        # A field as text ("" for anything that is not a string).
        return value if isinstance(value, str) else ""

    @staticmethod
    def _stamp(value: object):
        # An ISO time from the index as an aware datetime, or the oldest possible time.
        return Clock.parse_ts(value) or FLOOR

    @staticmethod
    def _harness(name: object) -> str:
        # The display name of a harness id.
        return {"claude-code": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "gemini-cli": "Gemini CLI", "qwen-code": "Qwen Code"}.get(str(name), str(name))
