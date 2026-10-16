"""This skill's settings for `vidbyte-cli agents suggest run`: typed flags, categories, count, and files.

`completed`, `in_progress`, `avoid`, `mistakes`, and `forbidden` values are suppression needles in the
CLI (an idea whose title, summary, first action, or actions contain one is dropped), so every tagged
value starts with its digest tags and the focus file tells the model never to copy a value verbatim.
"""

from __future__ import annotations

from collections import OrderedDict

from sessionkit.runner import InputsValidator, SuggestArgv

# inputs.json key -> (vidbyte-cli flag, most values this skill passes), in the order they are passed.
FLAG_KINDS = OrderedDict(
    [
        ("in_progress", ("--in-progress", 8)),
        ("decision", ("--decision", 5)),
        ("blockers", ("--blockers", 5)),
        ("completed", ("--completed", 5)),
        ("context", ("--context", 4)),
        ("constraint", ("--constraint", 3)),
        ("avoid", ("--avoid", 3)),
    ]
)
TAGGED_KINDS = {"in_progress", "decision", "blockers", "completed", "context"}
REQUIRED_ANY = ("in_progress", "decision", "blockers")
TAG_PATTERN = r"^\[((?:[SP]\d+)(?:[ ,]+[SP]\d+)*|all)\]\s+\S"
CATEGORIES = ("completion", "continuation", "verification", "stop_or_defer")
DEFAULT_COUNT = 15
MIN_COUNT = 2
MAX_COUNT = 30
MAX_GOAL_CHARS = 700
MAX_VALUE_CHARS = 500
DIGEST_FILE = "digest.md"
FOCUS_FILE = "unfinished-work-checklist.md"
INDEX_FILE = "sessions.json"
INPUTS_FILE = "inputs.json"
RESULT_FILE = "result.json"
CHECKLIST_FILE = "checklist.md"
PROJECT_KEY = "unfinished-agent-work"


class SuggestConfig:
    """Builds the validator and the argv for this skill from sessionkit's generic runner."""

    @staticmethod
    def validator(known_tags: set, redactor) -> InputsValidator:
        # An InputsValidator with this skill's flag kinds, tag rule, and limits.
        return InputsValidator(
            FLAG_KINDS, TAGGED_KINDS, TAG_PATTERN, known_tags, redactor,
            max_goal_chars=MAX_GOAL_CHARS, max_value_chars=MAX_VALUE_CHARS, required_any=REQUIRED_ANY,
        )

    @staticmethod
    def argv(exe: str, inputs: dict, count: int, dry_run: bool) -> list:
        # The exact `agents suggest run` command line, run from inside the output folder.
        return SuggestArgv.build(
            exe, inputs["goal"], CATEGORIES, count, FLAG_KINDS, inputs["values"],
            [DIGEST_FILE], [FOCUS_FILE], inputs.get("project"), dry_run, horizon="any",
        )
