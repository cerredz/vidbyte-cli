"""Maps each proposed skill back to the corrections behind it, checks its slug and description, and writes report.md.

The Suggestion Agent's ideas have fixed fields, so the focus file asks for the skill specification in
a fixed shape: the slug in backticks in the title, "Description:", "Got wrong:", and "Correct:" in
the summary, "Rule:" and "Step:" suggested actions, and labelled considerations. The parser reads
that shape and falls back gracefully, listing a problem whenever it had to guess.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from pathlib import Path

from sessionkit.model import Clock, TextTools
from sessionkit.runner import CommandIO

from .digest import Plural

TAG_GROUP_RE = re.compile(r"\[([^\[\]\n]{1,200})\]")
TAG_RE = re.compile(r"^[CPS]\d+$")
LABEL_RE = re.compile(r"\b(Description|Got wrong|Correct(?: behaviou?r)?)\s*:\s*", re.I)
PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(Rule|Step)\s*:\s*", re.I)
CONSIDERATION_RE = re.compile(r"^\s*(Where|Trigger|Do not fire|Evidence|Overlap|Risk|Alternative|Eval)\s*:\s*", re.I)
BACKTICK_RE = re.compile(r"`([^`\s]{1,80})`")
RESERVED = ("claude", "anthropic")


class SkillSpec:
    """The skill specification carried by one idea."""

    def __init__(self) -> None:
        # Empty fields; SkillIdeaParser fills them.
        self.slug = ""
        self.slug_source = ""
        self.title = ""
        self.description = ""
        self.got_wrong = ""
        self.correct = ""
        self.rules: list = []
        self.steps: list = []
        self.where: list = []
        self.evals: list = []
        self.considerations: list = []
        self.completion = ""
        self.first_action = ""
        self.problems: list = []


class SkillIdeaParser:
    """Reads the skill specification out of one idea of a suggestions.result document."""

    SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def parse(self, idea: dict) -> SkillSpec:
        # Fills a SkillSpec from the idea's title, summary, actions, and considerations, listing every guess as a problem.
        spec = SkillSpec()
        title = str(idea.get("title") or "")
        spec.slug, spec.slug_source = self.slug(title)
        spec.title = self.plain_title(title)
        parts = self.summary_parts(str(idea.get("summary") or ""))
        spec.description = parts.get("description", "")
        spec.got_wrong = parts.get("got wrong", "")
        spec.correct = parts.get("correct", "")
        if not spec.description:
            spec.description = self.first_use_when(str(idea.get("summary") or ""))
            if spec.description:
                spec.problems.append("The summary has no 'Description:' label; the first 'Use when' sentence was used.")
        if not spec.got_wrong:
            spec.got_wrong = str(idea.get("summary") or "")
            spec.problems.append("The summary has no 'Got wrong:' label; the whole summary is shown instead.")
        for action in idea.get("suggested_actions") or []:
            match = PREFIX_RE.match(str(action))
            text = str(action)[match.end():].strip() if match else str(action).strip()
            (spec.rules if match and match.group(1).lower() == "rule" else spec.steps).append(text)
        for item in idea.get("considerations") or []:
            match = CONSIDERATION_RE.match(str(item))
            label = match.group(1).lower() if match else ""
            text = str(item)[match.end():].strip() if match else str(item).strip()
            spec.considerations.append((label, text))
            if label in ("where", "trigger", "do not fire"):
                spec.where.append(text if label == "where" else "%s: %s" % (label.capitalize(), text))
            elif label == "eval":
                spec.evals.append(text)
        spec.completion = str(idea.get("completion_criteria") or "")
        spec.first_action = str(idea.get("first_action") or "")
        spec.problems.extend(self.slug_problems(spec.slug, spec.slug_source))
        spec.problems.extend(self.description_problems(spec.description))
        return spec

    def slug(self, title: str) -> tuple:
        # The first backticked slug in the title, else a kebab-case slug derived from the title.
        for token in BACKTICK_RE.findall(title):
            if self.SLUG_RE.match(token):
                return token, "title"
        words = re.findall(r"[a-z0-9]+", TAG_GROUP_RE.sub(" ", title).lower())
        slug = ""
        for word in words:
            candidate = (slug + "-" + word) if slug else word
            if len(candidate) > 64:
                break
            slug = candidate
        return slug, "derived"

    @staticmethod
    def plain_title(title: str) -> str:
        # The title without its leading "`slug`:" part and without tags.
        value = re.sub(r"^\s*`[^`]+`\s*[:\-–—]\s*", "", title)
        return " ".join(TAG_GROUP_RE.sub(" ", value).split()) or title.strip()

    @staticmethod
    def summary_parts(summary: str) -> dict:
        # Splits "Description: ... Got wrong: ... Correct: ..." into a dict keyed by lower-case label.
        parts: dict = {}
        pieces = LABEL_RE.split(summary)
        for position in range(1, len(pieces) - 1, 2):
            label = pieces[position].lower()
            label = "correct" if label.startswith("correct") else label
            parts[label] = " ".join(pieces[position + 1].split()).strip(" |;")
        return parts

    @staticmethod
    def first_use_when(text: str) -> str:
        # The first sentence that starts with "Use when", or "".
        match = re.search(r"(Use when\b.*?(?:\.(?=\s|$)|$))", text, re.S)
        return " ".join(match.group(1).split()) if match else ""

    def slug_problems(self, slug: str, source: str) -> list:
        # The reasons a slug cannot be a skill folder name.
        problems = []
        if source == "derived":
            problems.append("The title has no `slug` in backticks; '%s' was derived from the title." % slug)
        if not slug or not self.SLUG_RE.match(slug):
            problems.append("The slug must be lowercase words joined by single hyphens.")
        if len(slug) > 64:
            problems.append("The slug is longer than 64 characters.")
        if any(word in slug for word in RESERVED):
            problems.append("The slug contains a reserved word (claude or anthropic), which skill uploads refuse.")
        return problems

    @staticmethod
    def description_problems(description: str) -> list:
        # The reasons a description cannot go into SKILL.md frontmatter as it is.
        if not description:
            return ["The idea has no 'Use when ...' description."]
        problems = []
        if not description.lower().startswith("use when"):
            problems.append("The description does not start with 'Use when'.")
        if len(description) > 1024:
            problems.append("The description has %d characters; the limit is 1,024." % len(description))
        if "<" in description or ">" in description:
            problems.append("The description contains < or >, which skill frontmatter refuses.")
        return problems


class IdeaReporter:
    """Joins result.json with corrections.json and writes report.md."""

    TEXT_FIELDS = ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")
    LIST_FIELDS = ("suggested_actions", "considerations", "decision_points", "assumptions")

    def __init__(self, out_dir: Path, redactor) -> None:
        # Reads both documents from the output folder.
        self.out_dir = Path(out_dir)
        self.redact = redactor
        self.parser = SkillIdeaParser()
        document = CommandIO.load_json(self.out_dir / "result.json", "result.json")
        if not isinstance(document, dict) or document.get("kind") != "suggestions.result":
            raise SystemExit(CommandIO.usage_error("result.json is not a suggestions.result document; run `suggest --paid` first."))
        self.data = document.get("data") or {}
        self.index = CommandIO.load_json(self.out_dir / "corrections.json", "corrections.json")
        self.scenarios = {s["tag"]: s for s in self.index.get("scenarios", [])}
        self.sessions = {s["tag"]: s for s in self.index.get("sessions", [])}
        self.prompts = {p["tag"]: p for p in self.index.get("prompts", [])}
        self.existing = {str(e.get("name")) for e in self.index.get("skills_found", [])}

    def build(self) -> dict:
        # Maps every idea, checks it, writes report.md, and returns the JSON summary.
        ideas = [self.map_idea(idea) for idea in self.data.get("ideas") or [] if isinstance(idea, dict)]
        slugs = Counter(i["slug"] for i in ideas if i["slug"])
        for item in ideas:
            if item["slug"] and slugs[item["slug"]] > 1:
                item["problems"].append("Another idea proposes the same slug; merge them or rename one.")
        cited = {tag for item in ideas if item["grounded"] for tag in item["scenarios"]}
        uncovered = [{"tag": s["tag"], "label": s["label"], "corrections": s["corrections"], "sessions": s["sessions"]}
                     for s in self.index.get("scenarios", []) if s.get("full") and s["tag"] not in cited]
        report_path = self.out_dir / "report.md"
        TextTools.write_lf(report_path, self.render(ideas, uncovered))
        return {
            "result_file": str(self.out_dir / "result.json"),
            "report_file": str(report_path),
            "status": self.data.get("status"),
            "stop_reason": self.data.get("stop_reason"),
            "ideas": [{k: v for k, v in item.items() if k not in ("spec", "evidence")} for item in ideas],
            "ungrounded": sum(1 for i in ideas if not i["grounded"]),
            "uncovered_scenarios": uncovered,
            "warnings": self.data.get("warnings") or [],
        }

    def map_idea(self, idea: dict) -> dict:
        # One idea's tags, resolved evidence, skill fields, frequency, and problems.
        spec = self.parser.parse(idea)
        tags = self.tags_in(idea)
        known = [t for t in tags if t in self.scenarios or t in self.sessions or t in self.prompts]
        unknown = [t for t in tags if t not in known]
        prompt_tags = [t for t in known if t.startswith("P")]
        scenario_tags = list(OrderedDict.fromkeys([t for t in known if t.startswith("C")] + [self.prompts[t]["scenario"] for t in prompt_tags if self.prompts[t].get("scenario")]))
        grounded = bool([t for t in known if t[0] in "CP"])
        problems = list(spec.problems)
        if unknown:
            problems.append("Cites tags that are not in the digest: %s." % ", ".join(unknown))
        if not grounded:
            problems.append("Cites no scenario or correction from the digest: it has no evidence, so present it last and do not scaffold it.")
        updates = spec.slug in self.existing
        return {
            "id": idea.get("id"), "rank": idea.get("rank"), "title": self.redact(str(idea.get("title") or "")), "slug": spec.slug,
            "description": self.redact(spec.description), "grounded": grounded, "scenarios": scenario_tags, "prompts": prompt_tags,
            "sessions": [t for t in known if t.startswith("S")], "unknown_tags": unknown, "updates_existing": updates,
            "how_often": self.how_often(scenario_tags), "problems": problems, "spec": spec, "evidence": self.evidence(prompt_tags, scenario_tags),
        }

    def tags_in(self, idea: dict) -> list:
        # Every [C#]/[P#]/[S#] tag in the idea's text fields and its typed-input evidence, in first-seen order.
        texts = [str(idea.get(f) or "") for f in self.TEXT_FIELDS]
        for field in self.LIST_FIELDS:
            texts.extend(str(v) for v in idea.get(field) or [])
        handoff = idea.get("handoff") if isinstance(idea.get("handoff"), dict) else {}
        texts.extend(str(e.get("content") or "") for e in handoff.get("evidence") or [] if isinstance(e, dict))
        found: "OrderedDict[str, None]" = OrderedDict()
        for text in texts:
            for group in TAG_GROUP_RE.findall(text):
                for token in re.split(r"[\s,;/]+", group.strip()):
                    if TAG_RE.match(token):
                        found[token] = None
        return list(found)

    def how_often(self, scenario_tags: list) -> dict:
        # Frequency and cost summed over the cited scenarios, each counted once.
        totals = Counter()
        harnesses, projects, file_types = Counter(), Counter(), Counter()
        firsts, lasts = [], []
        for tag in scenario_tags:
            scenario = self.scenarios.get(tag)
            if not scenario:
                continue
            for key in ("corrections", "episodes", "sessions", "prompts_lost", "tool_calls_redone"):
                totals[key] += int(scenario.get(key) or 0)
            harnesses.update(scenario.get("harnesses") or {})
            projects.update(scenario.get("projects") or {})
            file_types.update(scenario.get("file_types") or {})
            if scenario.get("first"):
                firsts.append(scenario["first"])
            if scenario.get("last"):
                lasts.append(scenario["last"])
        return dict(totals, harnesses=dict(harnesses.most_common()), projects=dict(projects.most_common()),
                    file_types=dict(file_types.most_common()), first=min(firsts) if firsts else None, last=max(lasts) if lasts else None)

    def evidence(self, prompt_tags: list, scenario_tags: list) -> list:
        # The cited correction prompts, or the first corrections of the cited scenarios when the idea names no prompt.
        tags = [t for t in prompt_tags if self.prompts[t].get("role") == "correction"]
        source = "cited"
        if not tags:
            tags = [t for s in scenario_tags for t in (self.scenarios.get(s) or {}).get("correction_prompts", [])[:2]]
            source = "scenario"
        items = []
        for tag in tags[:6]:
            prompt = self.prompts[tag]
            session = self.sessions.get(prompt.get("session"), {})
            items.append({"tag": tag, "source": source, "harness": session.get("harness"), "project": session.get("project"),
                          "date": Clock.local_day(Clock.parse_ts(prompt.get("ts"))), "text": TextTools.one_line(prompt.get("text", ""), 300),
                          "agent_did": [TextTools.one_line(a, 90) for a in (prompt.get("corrected_actions") or [])[-4:]]})
        return items

    def render(self, ideas: list, uncovered: list) -> str:
        # report.md: one section per idea in rank order, then the scenarios no idea covers.
        lines = ["# Proposed skills from your corrections", ""]
        lines.append("%s; %d without evidence; %s with no proposed skill. Run status: %s (%s)." % (
            Plural.n(len(ideas), "idea"), sum(1 for i in ideas if not i["grounded"]), Plural.n(len(uncovered), "full scenario"), self.data.get("status"), self.data.get("stop_reason")))
        lines.append("")
        ordered = sorted(ideas, key=lambda i: (not i["grounded"], i.get("rank") or 999))
        for number, item in enumerate(ordered, 1):
            if not item["grounded"] and (number == 1 or ordered[number - 2]["grounded"]):
                lines.extend(["# Without evidence from your sessions", "", "These ideas cite no scenario or correction from the digest. Treat them as general ideas; they are not scaffolded.", ""])
            lines.extend(self.render_idea(number, item))
        if uncovered:
            lines.extend(["# Scenarios with no proposed skill", ""])
            for scenario in uncovered:
                lines.append("- [%s] %s - %s in %s" % (scenario["tag"], scenario["label"], Plural.n(scenario["corrections"], "correction"), Plural.n(scenario["sessions"], "session")))
            lines.append("")
        return self.redact("\n".join(lines).rstrip() + "\n")

    def render_idea(self, number: int, item: dict) -> list:
        # One idea's section of report.md.
        spec = item["spec"]
        often = item["how_often"]
        lines = ["## %d. `%s` - %s (%s)" % (number, item["slug"] or "no-slug", spec.title, item["id"]), ""]
        lines.append("**Description:** %s" % (spec.description or "(missing)"))
        lines.append("")
        if item["scenarios"]:
            lines.append("**How often:** %s in %s; %s spent correcting; %s corrected; %s to %s. Harnesses: %s. Projects: %s. Scenarios: %s." % (
                Plural.n(often.get("corrections", 0), "correction"), Plural.n(often.get("sessions", 0), "session"),
                Plural.n(often.get("prompts_lost", 0), "prompt"), Plural.n(often.get("tool_calls_redone", 0), "tool call"),
                Clock.local_day(Clock.parse_ts(often.get("first"))), Clock.local_day(Clock.parse_ts(often.get("last"))),
                ", ".join("%s %d" % kv for kv in often["harnesses"].items()) or "?", ", ".join("%s %d" % kv for kv in often["projects"].items()) or "?",
                ", ".join(item["scenarios"])))
            lines.append("")
        lines.append("**What the model got wrong:** %s" % spec.got_wrong)
        lines.append("")
        for evidence in item["evidence"]:
            did = "; ".join(evidence["agent_did"]) or "no tool call"
            lines.append("> [%s] %s · %s · %s: \"%s\" (agent did: %s)" % (evidence["tag"], evidence["harness"], evidence["project"] or "?", evidence["date"], evidence["text"], did))
        if item["evidence"]:
            lines.append("")
        lines.append("**Correct behavior:** %s" % (spec.correct or "(not stated)"))
        lines.append("")
        if spec.rules:
            lines.extend(["**Rules:**"] + ["- %s" % r for r in spec.rules] + [""])
        if spec.steps:
            lines.extend(["**Steps:**"] + ["%d. %s" % (n, s) for n, s in enumerate(spec.steps, 1)] + [""])
        if spec.where:
            lines.extend(["**Where it applies:**"] + ["- %s" % w for w in spec.where] + [""])
        lines.append("**Done when:** %s" % (spec.completion or "(not stated)"))
        for evaluation in spec.evals:
            lines.append("- Eval: %s" % evaluation)
        lines.append("")
        if spec.first_action:
            lines.extend(["**First action:** %s" % spec.first_action, ""])
        if item["updates_existing"]:
            lines.extend(["Note: a skill named `%s` already exists; this proposal updates it." % item["slug"], ""])
        if item["problems"]:
            lines.extend(["**Problems:**"] + ["- %s" % p for p in item["problems"]] + [""])
        return lines
