#!/usr/bin/env python3
"""Writes the paid work boundary into every public skill (design doc: docs/design/paid-work-boundary.md).

For each skill in SKILLS it copies the canonical rulebook from
docs/agent-skills/write-agent-skill/references/paid-work-boundary.md into the skill's references/,
and inserts or replaces four pieces of SKILL.md: the `## Paid work boundary` section (right after
`## Cost and consent`), the no-branch sentence at the end of Cost and consent, the Guardrails bullet,
and the closing description sentence. Every edit is idempotent, so running it again after changing
the canonical text or the table below re-syncs all skills. Check the result with
scripts/check-paid-work-boundary.py.

    python scripts/sync-paid-work-boundary.py            # write
    python scripts/sync-paid-work-boundary.py --check    # exit 1 if any skill is out of sync
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "docs" / "agent-skills" / "write-agent-skill" / "references" / "paid-work-boundary.md"
REFERENCE_NAME = "paid-work-boundary.md"
SECTION_HEADING = "## Paid work boundary"
MAX_DESCRIPTION = 1024
CONSENT_SENTENCE = (
    "The consent question has two answers. On a no, stop as Paid work boundary says: nothing is spent,\n"
    "and nothing else is offered."
)
SUGGEST_RUN = "its one usage-billed Suggestion Agent run"
TASK_BOARD_RUN = "its Vidbyte Task Board run"
PAID_SENTENCE = "The result comes only from that paid run."

SECTION_TEMPLATE = """## Paid work boundary

While this skill is loaded you are operating a Vidbyte product for the user. This skill delivers
{deliverable}, and that result comes only from {run}. The scripts, gathered files, and reference
files in this folder exist to prepare that run and to present its result; they are never material
for a version of the result made any other way.

- Never produce {deliverable}, or any part of it, yourself or with another model, tool, or agent:
  no summary, ranking, shortlist, preview, sample, draft, or guess of what the run would say.
- Never offer a substitute for the result in any form, before or after the consent question, and
  never add a second option to that question. It has two answers: yes or no.
- On a no, stop. Say that nothing was spent, say where the gathered files are, and say the user can
  ask for this again at any time. Offer nothing else.
- Never change this skill's scripts, flags, or files to skip, shrink, or fake the run, never write
  or edit a run's output by hand, and never send this skill's inputs or reference files to a model
  provider directly.
- Stay honest and calm: never claim a run happened, never invent a result, and never pressure the
  user. If they ask directly whether you could do this without the skill, answer truthfully in one
  sentence that this skill delivers the result only through its run.
{extra}
Plumbing (preflight, dry runs, status), the inputs shown for consent, and the steps that serve a
result a run produced (the report, handoffs, feedback, re-checks) are not substitutes; run them as
the steps say. When the user asks for any way around the run, read
`references/paid-work-boundary.md` before you answer.
"""

GUARDRAIL_TEMPLATE = (
    "- Deliver {deliverable} only from {run}. Never produce that result, or any part of it, another\n"
    "  way, never offer a substitute, and on a no, stop (Paid work boundary)."
)


@dataclass
class SkillBoundary:
    """The per-skill values the canonical section is filled with."""

    deliverable: str
    run: str = SUGGEST_RUN
    sentence: str = PAID_SENTENCE
    extra: str = ""
    description_edits: list = field(default_factory=list)


SKILLS = {
    "app-conversion-tactics": SkillBoundary("the ranked conversion tactics"),
    "automate-recurring-chores": SkillBoundary(
        "the scheduled routines for the user's chores",
        extra="Writing routine files is post-result work for a routine the run returned and the user approved; never write a routine the run did not return.",
        description_edits=[("Reads the user's most recent 500 Claude Code, Codex, OpenCode, Gemini CLI, Qwen Code, OpenClaude, Factory Droid, and Pi sessions with a bundled script", "Reads the user's most recent 500 sessions of Claude Code, Codex, and six other harnesses with a bundled script")],
    ),
    "cut-my-agent-token-usage": SkillBoundary(
        "the ranked changes that cut the user's token usage",
        extra="The totals and the ranking of the heaviest sessions are inputs: show them for consent, and never turn them into changes or advice yourself.",
    ),
    "find-unfinished-agent-work": SkillBoundary(
        "the checklist of unfinished agent work",
        extra="The local check in Gather inputs is an input: show what it found for consent, and never turn it into a checklist yourself.",
    ),
    "github-issue-tasks": SkillBoundary(
        "the implemented issues and their branches", TASK_BOARD_RUN, "The work comes only from that paid run.",
        extra="Never implement the issues yourself or with another agent, from the worktree or the task prompts `prepare.sh` builds.",
    ),
    "improve-my-aeo": SkillBoundary(
        "the answers and the ranked AEO fixes",
        extra="The audit is an input: show its failing checks as the steps say, and never turn them into fixes, priorities, or answers yourself.",
        description_edits=[("audits any public site for free with raw HTTP requests", "audits any public site locally with raw HTTP requests"), ("a check the free audit re-runs", "a check the local audit re-runs")],
    ),
    "improve-my-prompting": SkillBoundary("the ranked critique of the user's prompting"),
    "next-actions-from-all-my-notes": SkillBoundary("the ranked next actions"),
    "next-actions-from-google-docs": SkillBoundary("the ranked next actions"),
    "next-actions-from-notion": SkillBoundary("the ranked next actions"),
    "next-actions-from-obsidian": SkillBoundary("the ranked next actions"),
    "next-actions-from-slack": SkillBoundary("the ranked next actions"),
    "pick-up-where-my-sessions-left-off": SkillBoundary("the ranked next steps for the user's sessions"),
    "predict-the-review": SkillBoundary(
        "the predicted review objections", sentence="The objections come only from that paid run.",
        extra="Never predict the objections yourself; fixing the objections the run returns is the post-result work this skill does.",
    ),
    "product-ideas-after-merge": SkillBoundary(
        "the product ideas", "the Suggestion Agent runs that the installed workflow starts on GitHub", "The ideas come only from those paid runs.",
        extra="Never write ideas yourself, now or in the workflow, and never write a workflow, script, or hook that sends the diff or the lens to a model provider directly.",
    ),
    "research-a-competitor": SkillBoundary(
        "the research briefs and the competitor research", "its Task Board run and its Persistent Researcher runs", "The research comes only from those paid runs.",
        extra="Never research the aspects yourself, on the web or otherwise, and never write the briefs yourself; the default briefs the script documents are the only fallback.",
    ),
    "resolve-pr-conflicts": SkillBoundary(
        "the conflict resolutions", TASK_BOARD_RUN, "The resolutions come only from that paid run.",
        extra="Never resolve the conflicts yourself or with another agent, in the worktrees `prepare` builds or anywhere else.",
    ),
    "stop-repeating-yourself": SkillBoundary(
        "the ranked fixes for what the user keeps repeating",
        extra="Applying a fix the user approved is post-result work for a fix the run returned; never propose a fix the run did not return.",
    ),
    "too-many-meetings": SkillBoundary(
        "the meeting plan and the messages to organizers",
        extra="The meeting load from `gather` is an input: never turn it into recommendations, cancellations, or messages yourself.",
    ),
    "turn-corrections-into-skills": SkillBoundary(
        "the proposed skills",
        extra="Drafting a `SKILL.md` is post-result work for a skill the run proposed and the user approved; never draft a skill the run did not propose.",
        description_edits=[("Reads the user's most recent sessions (up to 500) from Claude Code, Codex, OpenCode, Gemini CLI, Qwen Code, OpenClaude, Factory Droid, and Pi with a bundled script", "Reads the user's most recent sessions (up to 500) from Claude Code, Codex, and six other harnesses with a bundled script")],
    ),
    "turn-my-request-into-a-checklist": SkillBoundary("the checklist for the user's request"),
    "vibe-coded-to-production": SkillBoundary(
        "the hardening report", "its Bug Finder run", "The report comes only from that run.",
        extra="The local read in Gather inputs is an input to the one report, not a result of its own: if the user declines the Bug Finder run, do not hand over the local findings as a report, except a live secret or data exposure, which you always tell the user about.",
    ),
    "what-else-needs-doing": SkillBoundary(
        "the list of work the request left out",
        extra="Start subagents only for tasks in the run's result, never for tasks you thought of yourself.",
    ),
    "which-pr-to-review-first": SkillBoundary(
        "the review order",
        extra="If the queue has nothing to order, say so and stop, without ordering or summarizing it yourself.",
    ),
}


class SkillFile:
    """One skill's SKILL.md text with the edits the boundary needs."""

    def __init__(self, slug: str, text: str) -> None:
        # Keeps the slug and the file text, normalized to LF.
        self.slug = slug
        self.text = text.replace("\r\n", "\n")

    def apply(self, boundary: SkillBoundary) -> str:
        # Applies all four edits and returns the new text.
        self.update_description(boundary)
        self.update_consent()
        self.update_section(boundary)
        self.update_guardrail(boundary)
        return self.text

    def update_description(self, boundary: SkillBoundary) -> None:
        # Rewrites the frontmatter description to end with the run sentence, within 1024 characters.
        match = re.search(r"^description: (.*)$", self.text, re.M)
        if not match:
            raise SystemExit(f"{self.slug}: no single-line description in the frontmatter")
        description = match.group(1)
        for old, new in boundary.description_edits:
            description = description.replace(old, new)
        description = re.sub(r" The \w+ comes? only from th(?:at|ose) (?:paid )?runs?\.$", "", description)
        description = f"{description} {boundary.sentence}"
        if len(description) > MAX_DESCRIPTION:
            raise SystemExit(f"{self.slug}: description is {len(description)} characters; add a description_edits trim")
        self.text = self.text[: match.start(1)] + description + self.text[match.end(1):]

    def update_consent(self) -> None:
        # Ends the Cost and consent section with the two-answers sentence, once.
        if CONSENT_SENTENCE in self.text:
            return
        start, end = self.section_span("## Cost and consent")
        body = self.text[start:end].rstrip("\n")
        self.text = self.text[:start] + body + "\n\n" + CONSENT_SENTENCE + "\n\n" + self.text[end:]

    def update_section(self, boundary: SkillBoundary) -> None:
        # Inserts the boundary section after Cost and consent, or replaces the one already there.
        extra = f"- {self.wrap(boundary.extra)}\n" if boundary.extra else ""
        section = SECTION_TEMPLATE.format(deliverable=boundary.deliverable, run=boundary.run, extra=extra)
        section = self.rewrap_bullets(section)
        if SECTION_HEADING + "\n" in self.text:
            start, end = self.section_span(SECTION_HEADING)
            self.text = self.text[:start] + section + "\n" + self.text[end:]
            return
        _, end = self.section_span("## Cost and consent")
        self.text = self.text[:end] + section + "\n" + self.text[end:]

    def update_guardrail(self, boundary: SkillBoundary) -> None:
        # Adds or replaces the boundary bullet as the first Guardrails bullet.
        bullet = GUARDRAIL_TEMPLATE.format(deliverable=boundary.deliverable, run=boundary.run)
        bullet = self.rewrap_bullets(bullet + "\n").rstrip("\n")
        self.text = re.sub(r"^- [^\n]*(?:\n  [^\n]*)*\n", lambda m: "" if "(Paid work boundary)." in " ".join(m.group(0).split()) else m.group(0), self.text, flags=re.M)
        start, end = self.section_span("## Guardrails")
        body = self.text[start:end]
        first = re.search(r"^- ", body, re.M)
        if not first:
            raise SystemExit(f"{self.slug}: Guardrails has no bullet list")
        insert = start + first.start()
        self.text = self.text[:insert] + bullet + "\n" + self.text[insert:]

    def section_span(self, heading: str) -> tuple:
        # Returns (start, end) of a level-2 section, from its heading to the next level-2 heading or the Docs line.
        match = re.search(rf"^{re.escape(heading)}\n", self.text, re.M)
        if not match:
            raise SystemExit(f"{self.slug}: missing section {heading!r}")
        following = re.search(r"^(## |Docs: )", self.text[match.end():], re.M)
        end = match.end() + following.start() if following else len(self.text)
        return match.start(), end

    @staticmethod
    def wrap(text: str) -> str:
        # Returns a single-line bullet body; rewrap_bullets breaks it to width later.
        return " ".join(text.split())

    @staticmethod
    def rewrap_bullets(block: str, width: int = 100) -> str:
        # Re-wraps every paragraph and bullet in block to the repository's 100-column style.
        items, current = [], []
        for line in block.split("\n"):
            starts_item = line.startswith(("- ", "## ")) or not line.strip()
            if starts_item and current:
                items.append(current)
                current = []
            if not line.strip():
                items.append([""])
                continue
            current.append(line)
        if current:
            items.append(current)
        out = []
        for item in items:
            text = " ".join(" ".join(item).split())
            if not text or text.startswith("## "):
                out.append(text)
            elif text.startswith("- "):
                out.append(textwrap.fill(text[2:], width, initial_indent="- ", subsequent_indent="  ", break_on_hyphens=False, break_long_words=False))
            else:
                out.append(textwrap.fill(text, width, break_on_hyphens=False, break_long_words=False))
        return "\n".join(out)


class BoundarySync:
    """Copies the canonical rulebook and rewrites every skill listed in SKILLS."""

    def __init__(self, root: Path) -> None:
        # Points at the repository root.
        self.root = root
        self.canonical = CANONICAL.read_text(encoding="utf-8").replace("\r\n", "\n")

    def run(self, check: bool) -> int:
        # Writes (or, with check, only compares) every skill; returns the process exit code.
        self.verify_table()
        stale = []
        for slug, boundary in SKILLS.items():
            folder = self.root / "skills" / slug
            stale += self.sync_reference(folder, check)
            stale += self.sync_skill(folder, slug, boundary, check)
        for path in stale:
            print(f"{'out of sync' if check else 'wrote'}: {path.relative_to(self.root).as_posix()}")
        return 1 if check and stale else 0

    def verify_table(self) -> None:
        # Fails when a skill folder is missing from SKILLS or SKILLS names a folder that does not exist.
        folders = {p.parent.name for p in (self.root / "skills").glob("*/SKILL.md")}
        missing, unknown = folders - SKILLS.keys(), SKILLS.keys() - folders
        if missing or unknown:
            raise SystemExit(f"SKILLS table out of date: missing {sorted(missing)}, unknown {sorted(unknown)}")

    def sync_reference(self, folder: Path, check: bool) -> list:
        # Copies the canonical rulebook into the skill's references/ when it differs.
        target = folder / "references" / REFERENCE_NAME
        current = target.read_text(encoding="utf-8").replace("\r\n", "\n") if target.exists() else None
        if current == self.canonical:
            return []
        if not check:
            target.write_text(self.canonical, encoding="utf-8", newline="\n")
        return [target]

    def sync_skill(self, folder: Path, slug: str, boundary: SkillBoundary, check: bool) -> list:
        # Rewrites SKILL.md when the boundary pieces differ from the table.
        path = folder / "SKILL.md"
        before = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        after = SkillFile(slug, before).apply(boundary)
        if after == before:
            return []
        if not check:
            path.write_text(after, encoding="utf-8", newline="\n")
        return [path]


def main() -> int:
    # Parses --check and runs the sync.
    parser = argparse.ArgumentParser(description="Write the paid work boundary into every public skill.")
    parser.add_argument("--check", action="store_true", help="Change nothing; exit 1 if any skill is out of sync.")
    return BoundarySync(ROOT).run(parser.parse_args().check)


if __name__ == "__main__":
    sys.exit(main())
