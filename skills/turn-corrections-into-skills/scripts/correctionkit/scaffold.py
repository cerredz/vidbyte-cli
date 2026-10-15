"""Renders a draft SKILL.md from one proposed skill and writes it only when the user asked for it.

The draft always lands inside the output folder first (drafts/<slug>/SKILL.md). Writing into a real
skills folder needs --write --dest, never replaces an existing SKILL.md, and refuses an idea that has
no evidence or whose slug or description a skill loader would reject. Evidence tags are stripped and
every string is redacted again, so a draft never carries raw prompt text or a tag that means nothing
outside this run.
"""

from __future__ import annotations

import re
from pathlib import Path

from sessionkit.model import Clock, TextTools
from sessionkit.runner import CommandIO

from .digest import Plural
from .report import TAG_GROUP_RE, TAG_RE, SkillIdeaParser

PLACE_TAG_RE = re.compile(r"\b(in|from|of|like|see|at|as in)\s+\[([^\[\]\n]{1,200})\]")


class ScaffoldRefusal(Exception):
    """Raised when a draft must not be written; the message says why."""


class SkillScaffolder:
    """Builds SKILL.md text for one idea and guards the write."""

    MAX_WHERE = 6
    STORE_PARTS = ((".claude", "projects"), (".codex", "sessions"), (".codex", "archived_sessions"), ("opencode", "storage"), (".gemini", "tmp"), (".qwen", "tmp"),
                   (".openclaude", "projects"), (".factory", "sessions"), (".pi", "agent"))
    YAML_SPECIAL_RE = re.compile(r"(^[\s\"'&*!|>%@`#,\[\]{}?:-])|(: )|( #)|(\s$)")

    def __init__(self, out_dir: Path, redactor) -> None:
        # Reads result.json and corrections.json from the output folder.
        self.out_dir = Path(out_dir)
        self.redact = redactor
        self.parser = SkillIdeaParser()
        document = CommandIO.load_json(self.out_dir / "result.json", "result.json")
        if not isinstance(document, dict) or document.get("kind") != "suggestions.result":
            raise ScaffoldRefusal("result.json is not a suggestions.result document; run `suggest --paid` first.")
        self.ideas = {str(i.get("id")): i for i in (document.get("data") or {}).get("ideas") or [] if isinstance(i, dict)}
        index = CommandIO.load_json(self.out_dir / "corrections.json", "corrections.json")
        self.prompts = {p["tag"]: p for p in index.get("prompts", [])}
        self.scenarios = {s["tag"]: s for s in index.get("scenarios", [])}
        self.existing = {str(e.get("name")): e.get("path") for e in index.get("skills_found", [])}

    def render(self, idea_id: str, slug: str = None, description: str = None) -> dict:
        # Returns {idea, slug, text, problems, draft_path, existing}, saving the draft inside the output folder when the slug is usable.
        idea = self.ideas.get(idea_id)
        if idea is None:
            raise ScaffoldRefusal("No idea %s in result.json; the ids are: %s." % (idea_id, ", ".join(sorted(self.ideas)) or "none"))
        spec = self.parser.parse(idea)
        problems = [p for p in spec.problems if not self.overridden(p, slug, description)]
        if slug is not None:
            spec.slug = slug.strip()
            problems.extend(self.parser.slug_problems(spec.slug, "override"))
        if description is not None:
            spec.description = " ".join(description.split())
            problems.extend(self.parser.description_problems(spec.description))
        evidence = self.evidence_count(idea)
        if not evidence["grounded"]:
            problems.append("The idea cites no scenario or correction from the digest, so it has no evidence; it will not be written.")
        text = self.text(idea_id, spec, evidence)
        result = {"idea": idea_id, "slug": spec.slug, "text": text, "problems": problems, "draft_path": None,
                  "existing": self.existing.get(spec.slug)}
        if spec.slug and self.parser.SLUG_RE.match(spec.slug) and len(spec.slug) <= 64:
            draft = self.out_dir / "drafts" / spec.slug / "SKILL.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            TextTools.write_lf(draft, text)
            result["draft_path"] = str(draft)
        return result

    def write(self, rendered: dict, dest_root: Path) -> Path:
        # Writes <dest_root>/<slug>/SKILL.md; raises ScaffoldRefusal on problems, an existing file, or a session-store destination.
        if rendered["problems"]:
            raise ScaffoldRefusal("Not written; fix these first: " + " ".join(rendered["problems"]))
        root = Path(str(dest_root)).expanduser().resolve()
        if self.inside_store(root):
            raise ScaffoldRefusal("%s is inside a coding agent's session store, not a skills folder." % root)
        target = root / rendered["slug"] / "SKILL.md"
        if target.exists():
            raise ScaffoldRefusal("%s already exists; nothing was changed. Merge the draft at %s into it by hand, or pick another --slug." % (target, rendered["draft_path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        TextTools.write_lf(target, rendered["text"])
        return target

    @staticmethod
    def overridden(problem: str, slug, description) -> bool:
        # True when an override replaces the field a parser problem is about.
        about_slug = "slug" in problem.lower() and "description" not in problem.lower()
        about_description = "description" in problem.lower() or "Use when" in problem
        return (slug is not None and about_slug) or (description is not None and about_description)

    def evidence_count(self, idea: dict) -> dict:
        # Whether the idea cites a known scenario or correction, and how many corrections and sessions those cover.
        tags = []
        for value in [idea.get("title"), idea.get("summary"), idea.get("why_now"), idea.get("first_action"), idea.get("completion_criteria")] + list(idea.get("suggested_actions") or []) + list(idea.get("considerations") or []):
            for group in TAG_GROUP_RE.findall(str(value or "")):
                tags.extend(t for t in re.split(r"[\s,;/]+", group.strip()) if TAG_RE.match(t))
        scenario_tags = {t for t in tags if t in self.scenarios}
        scenario_tags.update(self.prompts[t].get("scenario") for t in tags if t in self.prompts and self.prompts[t].get("scenario") in self.scenarios)
        corrections = sum(int(self.scenarios[t].get("corrections") or 0) for t in scenario_tags)
        sessions = sum(int(self.scenarios[t].get("sessions") or 0) for t in scenario_tags)
        return {"grounded": bool(scenario_tags), "corrections": corrections, "sessions": sessions}

    def text(self, idea_id: str, spec, evidence: dict) -> str:
        # The SKILL.md text: frontmatter, why it exists, what to do instead, rules, steps, done-when, where it applies.
        clean = self.clean
        title = clean(spec.title) or spec.slug
        lines = ["---", "name: %s" % spec.slug, "description: %s" % self.yaml_scalar(clean(spec.description)), "---", "", "# %s" % title, ""]
        if spec.got_wrong:
            lines.extend(["Why this skill exists: %s" % clean(spec.got_wrong), ""])
        if spec.correct:
            lines.extend(["## Do this instead", "", clean(spec.correct), ""])
        if spec.rules:
            lines.extend(["## Rules", ""] + ["- %s" % clean(r) for r in spec.rules] + [""])
        if spec.steps:
            lines.extend(["## Steps", ""] + ["%d. %s" % (n, clean(s)) for n, s in enumerate(spec.steps, 1)] + [""])
        if spec.completion:
            lines.extend(["## Done when", "", clean(spec.completion), ""])
        where = [clean(w) for w in spec.where if clean(w)][: self.MAX_WHERE]
        if where:
            lines.extend(["## Where it applies", ""] + ["- %s" % w for w in where] + [""])
        lines.append("<!-- Drafted by turn-corrections-into-skills on %s from %s in %s (%s). Review it before you commit it. -->" % (
            Clock.local_stamp(Clock.now())[:10], Plural.n(evidence["corrections"], "correction"), Plural.n(evidence["sessions"], "session"), idea_id))
        return self.redact("\n".join(lines)) + "\n"

    def clean(self, text: str) -> str:
        # Removes evidence tags ("the request in [P2]" becomes "the request in an earlier session"), tidies the gaps, redacts, and capitalizes.
        value = str(text or "")
        value = PLACE_TAG_RE.sub(lambda m: m.group(1) + " an earlier session" if self.only_tags(m.group(2)) else m.group(0), value)
        value = TAG_GROUP_RE.sub(lambda m: "" if self.only_tags(m.group(1)) else m.group(0), value)
        value = re.sub(r"\(\s*\)", "", value)
        value = re.sub(r"[ \t]+([,.;:)])", r"\1", value)
        value = self.redact(" ".join(value.split()))
        return value[:1].upper() + value[1:]

    @staticmethod
    def only_tags(group: str) -> bool:
        # True when a [...] group holds only C#/P#/S# tags.
        tokens = [t for t in re.split(r"[\s,;/]+", group.strip()) if t]
        return bool(tokens) and all(TAG_RE.match(t) for t in tokens)

    def yaml_scalar(self, value: str) -> str:
        # The value as a YAML scalar: plain when safe, else double-quoted with backslashes and quotes escaped.
        if value and not self.YAML_SPECIAL_RE.search(value) and value.lower() not in ("true", "false", "null", "yes", "no", "on", "off", "~"):
            return value
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')

    def inside_store(self, root: Path) -> bool:
        # True when root is inside a known session store folder.
        parts = [p.lower() for p in root.parts]
        for first, second in self.STORE_PARTS:
            for position in range(len(parts) - 1):
                if parts[position] == first and parts[position + 1] == second:
                    return True
        return False
