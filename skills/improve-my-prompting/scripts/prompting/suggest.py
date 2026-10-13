"""This skill's Suggestion Agent configuration: categories, typed flags, files, and instructions."""

from __future__ import annotations

from collections import OrderedDict

from sessionkit.runner import InputsValidator, SuggestArgv

PROJECT_KEY = "improve-my-prompting"
CATEGORIES = ("learning", "goal_clarification", "quick_wins", "leverage", "simplification", "risk_prevention")
COUNT = 8
FILES = ("digest.md",)
INSTRUCTIONS = ("prompting-rubric.md",)
FLAG_KINDS = OrderedDict([
    ("outcomes", ("--outcomes", 5)),
    ("mistakes", ("--mistakes", 8)),
    ("context", ("--context", 4)),
    ("capability", ("--capability", 3)),
    ("success", ("--success", 3)),
    ("constraint", ("--constraint", 3)),
    ("avoid", ("--avoid", 3)),
])
TAGGED_KINDS = {"outcomes", "mistakes", "context"}  # success/constraint/avoid hold the user's own words; --avoid must match verbatim.
TAG_PATTERN = r"^\[((?:[PS]\d+)(?:[ ,]+[PS]\d+)*|all)\]\s+\S"
REQUIRED_ANY = ("mistakes", "outcomes")
MAX_GOAL_CHARS = 700
MAX_VALUE_CHARS = 500


class PromptingRun:
    """Builds the validator and the `agents suggest run` argv with this skill's settings."""

    @staticmethod
    def validator(known_tags: set, redactor) -> InputsValidator:
        # An InputsValidator for inputs.json, checking tags against sessions.json.
        return InputsValidator(FLAG_KINDS, TAGGED_KINDS, TAG_PATTERN, known_tags, redactor, MAX_GOAL_CHARS, MAX_VALUE_CHARS, REQUIRED_ANY)

    @staticmethod
    def argv(exe: str, inputs: dict, dry_run: bool) -> list:
        # The full command line, run from inside the output folder so the file names stay relative.
        return SuggestArgv.build(exe, inputs["goal"], CATEGORIES, COUNT, FLAG_KINDS, inputs["values"], FILES, INSTRUCTIONS, inputs.get("project"), dry_run)
