"""Map each routine idea in result.json back to the chores, runs, and sessions it cites, and flag gaps.

Tags are read from the idea's own fields only. `handoff.evidence[].content` is the first 4,096
characters of the cited context item (the digest's top), so it would cite [C1] for every idea and is
never read for tags.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path

from sessionkit.model import TextTools
from sessionkit.runner import CommandIO

BRACKET_RE = re.compile(r"\[([^\[\]]{1,200})\]")
TAG_RE = re.compile(r"\b([CSP])(\d{1,4})\b")
ACTION_LABEL_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(ROUTINE PROMPT|SCHEDULE|HUMAN STEPS|DRY RUN|GO LIVE|TURN OFF)\s*:\s*", re.I)
CADENCE_WORDS = (
    ("biweekly", re.compile(r"\bbi-?weekly\b|\bevery (?:two|2|other) weeks?\b|\bfortnight(?:ly)?\b", re.I)),
    ("weekdays", re.compile(r"\bweekdays\b|\bevery weekday\b|\bmonday (?:to|through|-) friday\b", re.I)),
    ("daily", re.compile(r"\bdaily\b|\bevery day\b|\beach day\b", re.I)),
    ("weekly", re.compile(r"(?<!bi-)(?<!bi)\bweekly\b|\bevery week\b|\beach week\b|\bonce a week\b", re.I)),
    ("monthly", re.compile(r"\bmonthly\b|\bevery month\b|\beach month\b|\bonce a month\b|\b(?:first|last) business day of (?:the|each|every) month\b|\bend of (?:the|each|every) month\b", re.I)),
    ("quarterly", re.compile(r"\bquarterly\b|\bevery quarter\b|\beach quarter\b|\bonce a quarter\b", re.I)),
)
COMPATIBLE = {"daily": {"daily", "weekdays"}, "weekdays": {"weekdays", "daily"}}
ELEMENTS = OrderedDict([
    ("routine prompt", ("ROUTINE PROMPT", re.compile(r"\broutine prompt\b|\bprompt (?:file|text)\b", re.I))),
    ("schedule", ("SCHEDULE", re.compile(r"\bcron(?:tab)?\b|\blaunchd\b|\blaunchctl\b|\bschtasks\b|\bTask Scheduler\b|Register-ScheduledTask|\bGitHub Actions?\b|\bschedule:", re.I))),
    ("human step", ("HUMAN STEPS", re.compile(r"\bhuman\b|\byou (?:confirm|approve|review|check)\b|\bby hand\b|\bmanual(?:ly)?\b|\bapprov(?:e|al)\b", re.I))),
    ("dry run", ("DRY RUN", re.compile(r"\bdry[- ]run\b", re.I))),
])
CONFIRM_RE = re.compile(r"\bconfirm\w*\b|\bapprov\w*\b|\bhuman\b|\breview (?:it |them |the \w+ )?before\b|\bask(?:s|ing)? (?:the user|you|me)\b|\bdraft\w*\b", re.I)


class IdeaReader:
    """Reads result.json ideas and resolves their tags against chores.json."""

    def __init__(self, result: dict, index: dict) -> None:
        # Indexes chores by tag and maps each session and prompt tag to the chore whose runs hold it.
        self.ideas = [i for i in (((result or {}).get("data") or {}).get("ideas") or []) if isinstance(i, dict)]
        self.chores = OrderedDict((c["tag"], c) for c in index.get("chores") or [] if isinstance(c, dict) and c.get("tag"))
        self.sessions = {s["tag"]: s for s in index.get("sessions") or [] if isinstance(s, dict)}
        self.owner = {}
        for tag, chore in self.chores.items():
            for run in chore.get("runs") or []:
                for session in run.get("sessions") or []:
                    self.owner.setdefault(session, tag)
            for prompt in chore.get("prompts") or []:
                self.owner.setdefault(prompt, tag)

    def idea(self, idea_id: str):
        # The idea with this id, or None.
        return next((i for i in self.ideas if i.get("id") == idea_id), None)

    @staticmethod
    def text(idea: dict) -> str:
        # Every field an idea writes itself, joined; handoff evidence is left out on purpose.
        parts = [idea.get(k) or "" for k in ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria", "effort_estimate")]
        for key in ("suggested_actions", "decision_points", "considerations", "assumptions", "dependencies"):
            parts.extend(str(v) for v in idea.get(key) or [])
        return "\n".join(str(p) for p in parts)

    def cited(self, idea: dict):
        # Returns (chore tags in first-cited order, valid tags, unknown tags) for one idea.
        chores, valid, unknown = [], [], []
        for group in BRACKET_RE.findall(self.text(idea)):
            for letter, number in TAG_RE.findall(group):
                tag = letter + number
                chore = tag if letter == "C" and tag in self.chores else self.owner.get(tag)
                if chore is None:
                    unknown.append(tag)
                    continue
                valid.append(tag)
                if chore not in chores:
                    chores.append(chore)
        return chores, valid, sorted(set(unknown))

    @staticmethod
    def labelled_action(idea: dict, label: str):
        # The suggested action that starts with "LABEL:", without the label, or None.
        for action in idea.get("suggested_actions") or []:
            match = ACTION_LABEL_RE.match(str(action))
            if match and match.group(1).upper() == label:
                return str(action)[match.end():].strip()
        return None


class RoutineReporter:
    """Builds report.md and the JSON summary for one output folder."""

    def __init__(self, out_dir: Path, redactor) -> None:
        # Reads result.json and chores.json from out_dir.
        self.out_dir = Path(out_dir).expanduser()
        self.redact = redactor

    def build(self) -> dict:
        # Returns {"ideas", "uncovered_chores", "already_automated", "report"}; exits 2 when result.json is not a suggestions result.
        result = CommandIO.load_json(self.out_dir / "result.json", "result.json")
        if not isinstance(result, dict) or result.get("kind") != "suggestions.result":
            raise SystemExit(CommandIO.usage_error("result.json is not a suggestions.result document; run `suggest --paid` first, or pass the folder that holds its result."))
        index = CommandIO.load_json(self.out_dir / "chores.json", "chores.json")
        reader = IdeaReader(result, index)
        ideas = self._ideas(reader)
        covered = {tag for entry in ideas for tag in entry["chores"]}
        uncovered = [OrderedDict([("tag", t), ("label", c.get("label")), ("cadence", (c.get("cadence") or {}).get("label"))]) for t, c in reader.chores.items() if t not in covered]
        automated = [OrderedDict([("label", e.get("label")), ("runs", e.get("runs")), ("days", e.get("days")), ("evidence", e.get("automation_evidence"))]) for e in index.get("already_automated") or []]
        TextTools.write_lf(self.out_dir / "report.md", self._markdown(reader, ideas, uncovered, automated, index))
        return OrderedDict([("ideas", ideas), ("uncovered_chores", uncovered), ("already_automated", automated), ("irregular_repeats", len(index.get("irregular") or [])), ("report", str(self.out_dir / "report.md"))])

    def _ideas(self, reader: IdeaReader) -> list:
        # One entry per idea in rank order, with its chores and flags.
        seen = set()
        out = []
        for idea in sorted(reader.ideas, key=lambda i: (i.get("rank") or 999, i.get("id") or "")):
            chores, valid, unknown = reader.cited(idea)
            flags = []
            if not valid:
                flags.append("grounded: false")
            if len(chores) > 1:
                flags.append("merged_chores: %s" % " ".join(chores))
            alternative = "alternative" in str(idea.get("title") or "").lower()
            for tag in chores:
                if tag in seen and not alternative:
                    flags.append("duplicate_chore: %s is already covered by a higher-ranked idea" % tag)
            seen.update(chores)
            flags.extend(self._cadence_flags(reader, idea, chores))
            missing = self._missing(reader, idea)
            if missing:
                flags.append("missing: %s" % ", ".join(missing))
            flags.extend(self._external_flags(reader, idea, chores))
            if unknown:
                flags.append("unknown_tags: %s" % " ".join(unknown))
            out.append(OrderedDict([
                ("id", idea.get("id")), ("rank", idea.get("rank")), ("title", idea.get("title")), ("category", idea.get("primary_category")),
                ("horizon", idea.get("horizon")), ("chores", chores), ("grounded", bool(valid)), ("tags", valid),
                ("routine_prompt", bool(reader.labelled_action(idea, "ROUTINE PROMPT"))), ("missing", missing), ("flags", flags),
            ]))
        return out

    @staticmethod
    def _cadence_flags(reader: IdeaReader, idea: dict, chores: list) -> list:
        # cadence_mismatch when the idea's title, summary, or SCHEDULE step names cadences and none is the chore's.
        text = "\n".join([idea.get("title") or "", idea.get("summary") or "", reader.labelled_action(idea, "SCHEDULE") or ""])
        named = set()
        for kind, pattern in CADENCE_WORDS:
            if pattern.search(text):
                named.add(kind)
                text = pattern.sub(" ", text)
        flags = []
        for tag in chores:
            kind = (reader.chores[tag].get("cadence") or {}).get("kind")
            if named and kind and not (named & COMPATIBLE.get(kind, {kind})):
                flags.append("cadence_mismatch: %s is %s, the idea names %s" % (tag, kind, ", ".join(sorted(named))))
        return flags

    @staticmethod
    def _missing(reader: IdeaReader, idea: dict) -> list:
        # The required routine elements the idea has neither as a labelled step nor in its words.
        text = IdeaReader.text(idea)
        return [name for name, (label, pattern) in ELEMENTS.items() if not reader.labelled_action(idea, label) and not pattern.search(text)]

    @staticmethod
    def _external_flags(reader: IdeaReader, idea: dict, chores: list) -> list:
        # unguarded_external_action when a cited chore has outside actions and the idea never names a confirmation.
        text = IdeaReader.text(idea)
        flags = []
        for tag in chores:
            actions = [e.get("action") for e in (reader.chores[tag].get("outputs") or {}).get("external_actions") or []]
            if actions and not CONFIRM_RE.search(text):
                flags.append("unguarded_external_action: %s (%s) and no confirmation step" % (tag, ", ".join(actions[:3])))
        return flags

    def _markdown(self, reader: IdeaReader, ideas: list, uncovered: list, automated: list, index: dict) -> str:
        # report.md: each idea with its chores, runs, resume commands, routine prompt excerpt, and flags.
        lines = ["# Recurring chore routines", "", "Every idea below is traced to the chores and sessions it cites. Flags are checks for you, not verdicts.", ""]
        for entry in ideas:
            idea = reader.idea(entry["id"]) or {}
            lines += ["## %s. %s (%s)" % (entry["rank"], self.redact(entry["title"]), entry["id"]), ""]
            if not entry["grounded"]:
                lines += ["Not grounded: this idea cites no chore, session, or prompt from the digest. Treat it as a general suggestion.", ""]
            for tag in entry["chores"]:
                lines.extend(self._chore_lines(reader, tag))
            prompt = reader.labelled_action(idea, "ROUTINE PROMPT")
            if prompt:
                lines += ["Routine prompt (excerpt): %s" % TextTools.one_line(self.redact(prompt), 600), ""]
            lines += ["Flags: %s" % ("; ".join(entry["flags"]) if entry["flags"] else "none"), ""]
        if uncovered:
            lines += ["## Chores no idea covers", ""] + ["- [%s] %s (%s)" % (c["tag"], c["label"], c["cadence"]) for c in uncovered] + [""]
        if automated:
            lines += ["## Repeats that look already automated (not proposed)", ""]
            lines += ["- %s: %d runs (%s); %s" % (e["label"], e["runs"] or 0, ", ".join((e["days"] or [])[-4:]), "; ".join(e["evidence"] or [])) for e in automated] + [""]
        irregular = index.get("irregular") or []
        if irregular:
            lines += ["%d other repeats had no steady cadence and were not sent (see chores.json, irregular)." % len(irregular), ""]
        return "\n".join(lines)

    def _chore_lines(self, reader: IdeaReader, tag: str) -> list:
        # The chore's cadence, run dates with session tags, and resume commands.
        chore = reader.chores[tag]
        cadence = chore.get("cadence") or {}
        lines = ["- [%s] %s: %s; next expected %s%s" % (tag, chore.get("label"), cadence.get("label"), cadence.get("next_expected"), " (overdue)" if cadence.get("overdue") else "")]
        runs = chore.get("runs") or []
        lines.append("  - Runs: %s" % ", ".join("%s [%s]" % (r.get("day"), " ".join(r.get("sessions") or [])) for r in runs[-8:]))
        for run in runs[-2:]:
            for session_tag in (run.get("sessions") or [])[:1]:
                session = reader.sessions.get(session_tag) or {}
                resume = session.get("resume") or {}
                env = " ".join("%s=%s" % (key, self.redact(value)) for key, value in sorted((resume.get("env") or {}).items()))
                command = resume.get("command") or resume.get("note") or ""
                lines.append("  - Resume [%s] (%s): %s%s" % (session_tag, session.get("harness"), (env + " ") if env else "", self.redact(command)))
        lines.append("")
        return lines
