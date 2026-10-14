"""This skill's `agents suggest run` settings, and the suggest and feedback commands built on sessionkit.runner."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

from sessionkit.redaction import Redactor
from sessionkit.runner import PROJECT_RE, CliInvoker, CommandIO, FeedbackArgv, InputsValidator, MAX_ARGV_CHARS, PaidRunGuard, RunReporter, SuggestArgv

FLAG_KINDS = OrderedDict([
    ("outcomes", ("--outcomes", 10)), ("context", ("--context", 6)), ("capability", ("--capability", 3)), ("risks", ("--risks", 8)),
    ("forbidden", ("--forbidden", 4)), ("success", ("--success", 3)), ("constraint", ("--constraint", 3)), ("avoid", ("--avoid", 3)),
])
TAGGED_KINDS = {"outcomes", "context", "risks", "forbidden"}
TAG_PATTERN = r"^\[((?:all|[CSP]\d+)(?:[ ,]+[CSP]\d+)*)\]"
CATEGORIES = ("delegation", "leverage", "risk_prevention")
PROJECT_KEY = "automate-recurring-chores"
NO_CLI = "vidbyte-cli is not on PATH. Follow references/setup.md step 1."


class SuggestPlan:
    """The count rule and the known tags for one output folder."""

    @staticmethod
    def count(chores_in_digest: int) -> int:
        # One idea per chore, between 2 and 10 (the CLI asks for at least 2).
        return max(2, min(10, chores_in_digest))

    @staticmethod
    def known_tags(index: dict) -> set:
        # The tags digest.md holds, as recorded in chores.json.
        return set(index.get("digest_tags") or []) if isinstance(index, dict) else set()

    @staticmethod
    def chores_in_digest(index: dict) -> int:
        # How many chores digest.md holds.
        return len(index.get("chores") or []) if isinstance(index, dict) else 0


class SuggestCommand:
    """`suggest --dry-run|--paid`: validate inputs.json, build the argv, and run vidbyte-cli inside the output folder."""

    def __init__(self, out_dir: Path, dry_run: bool, paid: bool, cli: str = None) -> None:
        # Holds the parsed options.
        self.out_dir = Path(out_dir).expanduser()
        self.dry_run = dry_run
        self.paid = paid
        self.cli = cli

    def run(self) -> int:
        # Returns the exit code: 2 for usage problems, 127 without a CLI, else vidbyte-cli's code.
        if self.dry_run == self.paid:
            return CommandIO.usage_error("Pass exactly one of --dry-run (free check) or --paid (the usage-billed run, only after the user said yes).")
        for name in ("digest.md", "chore-routines.md"):
            if not (self.out_dir / name).is_file():
                return CommandIO.usage_error("%s not found in %s. Run `gather` first and pass the out_dir it printed." % (name, self.out_dir))
        index = CommandIO.load_json(self.out_dir / "chores.json", "chores.json")
        inputs, problems = self._validated(index)
        if problems:
            CommandIO.print_json({"error": "inputs.json needs fixing", "problems": problems})
            for problem in problems:
                print("inputs.json: " + problem, file=sys.stderr)
            return 2
        invoker = CliInvoker(self.cli)
        exe = invoker.resolve()
        if not exe:
            print(json.dumps({"error": ("--cli %s was not found." % self.cli) if self.cli else NO_CLI}))
            return 127
        print("Using %s" % exe, file=sys.stderr)
        if self.paid and PaidRunGuard(self.out_dir).problem():
            return CommandIO.usage_error(PaidRunGuard(self.out_dir).problem())
        argv = self._argv(exe, inputs, index)
        length = SuggestArgv.length(argv)
        if length > MAX_ARGV_CHARS:
            return CommandIO.usage_error("The command would be %d characters; shorten the longest inputs.json values (%s) to stay under %d." % (length, self._longest(inputs), MAX_ARGV_CHARS))
        if self.paid:
            PaidRunGuard(self.out_dir).mark()
        print("Running in %s:\n%s" % (self.out_dir, invoker.display(argv)), file=sys.stderr)
        code, stdout_path, stderr_path, error = invoker.run(argv, self.out_dir, "dry-run.json" if self.dry_run else "result.json")
        CommandIO.print_json(RunReporter.summarize(code, stdout_path, stderr_path, error))
        return code

    def _validated(self, index: dict):
        # Validates inputs.json against this skill's flag kinds and the digest's tags.
        validator = InputsValidator(FLAG_KINDS, TAGGED_KINDS, TAG_PATTERN, SuggestPlan.known_tags(index), Redactor(Path.home()), required_any=("outcomes",))
        return validator.validate(CommandIO.load_json(self.out_dir / "inputs.json", "inputs.json"))

    def _argv(self, exe: str, inputs: dict, index: dict) -> list:
        # The exact command line, run from inside the output folder with relative file names.
        count = SuggestPlan.count(SuggestPlan.chores_in_digest(index))
        return SuggestArgv.build(exe, inputs["goal"], CATEGORIES, count, FLAG_KINDS, inputs["values"], ["digest.md"], ["chore-routines.md"], inputs["project"], self.dry_run, horizon="any")

    @staticmethod
    def _longest(inputs: dict) -> str:
        # Names the three longest values, so the agent knows what to shorten.
        sized = [(len(v), kind) for kind, values in inputs["values"].items() for v in values] + [(len(inputs["goal"]), "goal")]
        return ", ".join("%s (%d chars)" % (kind, n) for n, kind in sorted(sized, reverse=True)[:3])


class FeedbackCommand:
    """`feedback`: record the user's explicit accept or reject of one idea, with its exact title."""

    def __init__(self, out_dir: Path, idea: str, accept: bool, reason: str = None, project: str = None, cli: str = None) -> None:
        # Holds the parsed options.
        self.out_dir = Path(out_dir).expanduser()
        self.idea = idea
        self.verb = "accept" if accept else "reject"
        self.reason = reason
        self.project = project
        self.cli = cli

    def run(self) -> int:
        # Returns vidbyte-cli's exit code, 2 for usage problems, or 127 without a CLI.
        result = CommandIO.load_json(self.out_dir / "result.json", "result.json")
        ideas = ((result.get("data") or {}).get("ideas") or []) if isinstance(result, dict) else []
        match = [i for i in ideas if isinstance(i, dict) and i.get("id") == self.idea]
        if not match:
            return CommandIO.usage_error("No idea with id %s in result.json. Ids look like idea-001." % self.idea)
        project = self.project or self._inputs_project()
        if not project or not PROJECT_RE.match(project):
            return CommandIO.usage_error("No suggestion project. Create it first (see the skill's Deliver section), then pass --project or set project in inputs.json.")
        invoker = CliInvoker(self.cli)
        exe = invoker.resolve()
        if not exe:
            print(json.dumps({"error": ("--cli %s was not found." % self.cli) if self.cli else NO_CLI}))
            return 127
        argv = FeedbackArgv.build(exe, self.verb, project, match[0]["title"], self.reason or "", Redactor(Path.home()))
        print(invoker.display(argv), file=sys.stderr)
        return subprocess.call(argv)

    def _inputs_project(self):
        # The project key saved in inputs.json, if any.
        path = self.out_dir / "inputs.json"
        if not path.is_file():
            return None
        data = CommandIO.load_json(path, "inputs.json")
        return data.get("project") if isinstance(data, dict) else None

