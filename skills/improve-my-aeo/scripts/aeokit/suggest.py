"""This skill's Suggestion Agent configuration: categories, typed flags, files, instructions, and tags."""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path

from .runner import InputsValidator, SuggestArgv

PROJECT_KEY = "improve-my-aeo"
CATEGORIES = ("quick_wins", "risk_prevention", "leverage", "strategy", "verification")
COUNT = 8
FILES = ("audit.md", "aeo-checklist.md")
INSTRUCTIONS = ("aeo-rubric.md",)
MAX_INSTRUCTION_BYTES = 20_000  # vidbyte-cli InstructionsLimit.MAX_TOTAL_BYTES
FLAG_KINDS = OrderedDict([
    ("question", ("--question", 3)),
    ("mistakes", ("--mistakes", 8)),
    ("outcomes", ("--outcomes", 5)),
    ("context", ("--context", 4)),
    ("capability", ("--capability", 3)),
    ("success", ("--success", 3)),
    ("constraint", ("--constraint", 3)),
    ("avoid", ("--avoid", 3)),
])
TAGGED_KINDS = {"outcomes", "mistakes", "context"}  # question/success/constraint/avoid hold the user's own words.
TAG_ITEM = r"(?:E\d+|[A-Z]{2,3}-\d{2})"
TAG_PATTERN = r"^\[(" + TAG_ITEM + r"(?:[ ,]+" + TAG_ITEM + r")*|all)\]\s+\S"
CHECKLIST_ID_RE = re.compile(r"(?m)^###\s+([A-Z]{2,3}-\d{2})\b")
REQUIRED_ANY = ("mistakes", "outcomes", "question")
MAX_GOAL_CHARS = 700
MAX_VALUE_CHARS = 500


class AeoRun:
    """Builds the validator and the `agents suggest run` argv with this skill's settings."""

    @staticmethod
    def checklist_ids(checklist_path: Path) -> set:
        # Every item id ("### ACC-01 ...") defined in aeo-checklist.md.
        try:
            text = Path(checklist_path).read_text(encoding="utf-8")
        except OSError:
            return set()
        return set(CHECKLIST_ID_RE.findall(text))

    @staticmethod
    def validator(known_tags: set, redactor) -> InputsValidator:
        # An InputsValidator for inputs.json, checking tags against evidence.json and the checklist ids.
        return InputsValidator(FLAG_KINDS, TAGGED_KINDS, TAG_PATTERN, known_tags, redactor, MAX_GOAL_CHARS, MAX_VALUE_CHARS, REQUIRED_ANY)

    @staticmethod
    def argv(exe: str, inputs: dict, dry_run: bool) -> list:
        # The full command line, run from inside the output folder so the file names stay relative.
        return SuggestArgv.build(exe, inputs["goal"], CATEGORIES, COUNT, FLAG_KINDS, inputs["values"], FILES, INSTRUCTIONS, inputs.get("project"), dry_run)
