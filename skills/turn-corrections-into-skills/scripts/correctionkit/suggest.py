"""This skill's settings for `vidbyte-cli agents suggest run`: typed inputs, tags, categories, count, and files.

The generic parts (validation, argv order, the paid-run guard, the CLI call, the cost estimate) come
from sessionkit.runner; this module only says how turn-corrections-into-skills uses them.
"""

from __future__ import annotations

from collections import OrderedDict

from sessionkit.runner import InputsValidator, SuggestArgv


class CorrectionSuggestConfig:
    """Flag kinds, caps, tag rules, categories, and file names for one corrections run."""

    FLAG_KINDS = OrderedDict([
        ("outcomes", ("--outcomes", 4)),
        ("mistakes", ("--mistakes", 8)),
        ("context", ("--context", 4)),
        ("capability", ("--capability", 2)),
        ("success", ("--success", 3)),
        ("constraint", ("--constraint", 3)),
        ("avoid", ("--avoid", 3)),
    ])
    TAGGED = {"outcomes", "mistakes", "context"}
    TAG_PATTERN = r"^\[((?:[CPS]\d+|all|setup)(?:[ ,]+(?:[CPS]\d+|all|setup))*)\]"
    CATEGORIES = ("risk_prevention", "leverage", "learning", "verification")
    DIGEST = "digest.md"
    FOCUS = "skills-from-corrections.md"
    INDEX = "corrections.json"
    PROJECT = "turn-corrections-into-skills"
    MAX_COUNT = 12

    def known_tags(self, index: dict) -> set:
        # Every tag corrections.json resolves, plus the two group labels the context values may use.
        tags = {"all", "setup"}
        tags.update(s.get("tag") for s in index.get("scenarios", []) if isinstance(s, dict))
        tags.update(s.get("tag") for s in index.get("sessions", []) if isinstance(s, dict))
        tags.update(p.get("tag") for p in index.get("prompts", []) if isinstance(p, dict))
        tags.discard(None)
        return tags

    def count(self, index: dict) -> int:
        # One idea per full scenario, clamped to the CLI's minimum of 2 and this skill's cap of 12.
        full = sum(1 for s in index.get("scenarios", []) if isinstance(s, dict) and s.get("full"))
        return max(2, min(full, self.MAX_COUNT))

    def validator(self, index: dict, redactor) -> InputsValidator:
        # An InputsValidator that requires tags on outcomes, mistakes, and context, and at least one mistakes value.
        return InputsValidator(self.FLAG_KINDS, self.TAGGED, self.TAG_PATTERN, self.known_tags(index), redactor, required_any=("mistakes",))

    def argv(self, exe: str, inputs: dict, index: dict, dry_run: bool) -> list:
        # The exact command line, run from inside the output folder so both file paths stay relative.
        return SuggestArgv.build(exe, inputs["goal"], self.CATEGORIES, self.count(index), self.FLAG_KINDS, inputs["values"],
                                 [self.DIGEST], [self.FOCUS], inputs.get("project"), dry_run, horizon="any")
