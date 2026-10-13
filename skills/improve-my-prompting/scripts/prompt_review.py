#!/usr/bin/env python3
"""Critique how a developer prompts their coding agents, from their own recent sessions.

Subcommands:
  gather    Read up to 500 recent Claude Code, Codex, OpenCode, Gemini CLI, and Qwen Code sessions
            (local, read-only, free) and write digest.md, sessions.json, inputs.json, and
            prompting-rubric.md into an output folder.
  suggest   Validate inputs.json and run `vidbyte-cli agents suggest run` from inside that folder
            (--dry-run is free; --paid is billed by usage and needs the user's yes).
  report    Map every idea in result.json back to the tagged prompts, signals, and rubric items (free).
  feedback  Record the user's explicit accept or reject of one idea (free).

Standard library only; Python 3.8 or newer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sessionkit.collector import CollectOptions, SessionCollector  # noqa: E402
from sessionkit.model import Clock, TextTools  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402
from sessionkit.runner import MAX_ARGV_CHARS, MAX_TOTAL_TOKENS, PROJECT_RE, CliInvoker, CommandIO, FeedbackArgv, PaidRunGuard, RunReporter, SuggestArgv  # noqa: E402
from prompting.digest import DEFAULT_MAX_CHARS, MAX_MAX_CHARS, MIN_MAX_CHARS, DigestTooSmall, DigestWriter  # noqa: E402
from prompting.report import IdeaReporter, ReportError  # noqa: E402
from prompting.signals import SignalDetector, TemplateIndex  # noqa: E402
from prompting.suggest import PROJECT_KEY, PromptingRun  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
RUBRIC = SKILL_DIR / "references" / "prompting-rubric.md"
NO_CLI = "vidbyte-cli was not found. Follow references/setup.md step 1, or pass --cli <path to vidbyte-cli>."


class GatherCommand:
    """Reads the sessions, detects signals, and writes the digest files."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags.
        self.args = args

    def run(self) -> int:
        # Collects, detects, and writes; prints a JSON summary; exits 3 when no prompt was found.
        redactor = Redactor(Path.home())
        result = SessionCollector(CollectOptions.from_args(self.args)).collect()
        summary = {"read": result.summary(redactor)}
        if not result.sessions:
            summary["error"] = "No prompts found in the harness folders read (see read[]). Pass --claude-dir, --codex-home, --opencode-dir, --gemini-dir, or --qwen-dir if your sessions live elsewhere, or widen --days/--since."
            CommandIO.print_json(summary)
            return 3
        templates = TemplateIndex(result.sessions)
        detector = SignalDetector()
        signals = detector.detect(result.sessions, templates)
        overall = detector.overall(result.sessions, signals)
        out_dir = self._out_dir()
        try:
            written = DigestWriter(out_dir, redactor, self.args.max_chars, RUBRIC).write(result.sessions, signals, overall, self._window(result), summary["read"], templates)
        except (DigestTooSmall, ValueError) as error:
            return CommandIO.usage_error(str(error))
        summary.update(self._report(out_dir, written, signals, overall, redactor))
        CommandIO.print_json(summary)
        return 0

    def _out_dir(self) -> Path:
        # The --out folder, else a new timestamped folder under the system temp folder.
        if self.args.out:
            return Path(self.args.out).expanduser()
        return Path(tempfile.gettempdir()) / "improve-my-prompting" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    def _window(self, result) -> dict:
        # The collection window plus the first and last prompt days actually seen.
        stamps = [t.ts for s in result.sessions for t in s.turns if t.ts]
        return {
            "since": Clock.iso(result.since) or None,
            "until": Clock.iso(result.until),
            "max_sessions": result.max_sessions,
            "first_day": Clock.local_stamp(min(stamps))[:10] if stamps else "?",
            "last_day": Clock.local_stamp(max(stamps))[:10] if stamps else "?",
        }

    def _report(self, out_dir: Path, written: dict, signals: list, overall: dict, redactor: Redactor) -> dict:
        # The file paths, digest numbers, top signals, and redaction counts for stdout.
        return {
            "out_dir": str(out_dir),
            "files": {name: str(out_dir / name) for name in ("digest.md", "sessions.json", "inputs.json", "prompting-rubric.md")},
            "digest": written,
            "overall": {k: overall[k] for k in ("sessions", "prompts", "by_harness", "per_100_prompts")},
            "signals": [{"id": s.id, "prompts": s.prompt_count, "sessions": s.session_count, "templates": s.template_count, "rubric": list(s.definition.rubric)} for s in signals],
            "redactions": dict(redactor.counts),
            "next": "Review inputs.json, then: prompt_review.py suggest --out <out_dir> --dry-run",
        }


class SuggestCommand:
    """Validates inputs.json and runs the Suggestion Agent from inside the output folder."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags.
        self.args = args
        self.out_dir = Path(args.out).expanduser()

    def run(self) -> int:
        # Checks the folder and inputs, guards the paid run, runs the CLI, and prints its summary.
        if self.args.dry_run == self.args.paid:
            return CommandIO.usage_error("Pass exactly one of --dry-run (free check) or --paid (the usage-billed run, only after the user said yes).")
        problem = self._missing_files()
        if problem:
            return CommandIO.usage_error(problem)
        inputs, problems = self._validated_inputs()
        if problems:
            CommandIO.print_json({"error": "inputs.json needs fixing", "problems": problems})
            for line in problems:
                print("inputs.json: " + line, file=sys.stderr)
            return 2
        invoker = CliInvoker(self.args.cli)
        exe = invoker.resolve()
        if not exe:
            CommandIO.print_json({"error": NO_CLI})
            return 127
        guard = PaidRunGuard(self.out_dir)
        if self.args.paid and guard.problem():
            return CommandIO.usage_error(guard.problem())
        argv = PromptingRun.argv(exe, inputs, self.args.dry_run)
        if SuggestArgv.length(argv) > MAX_ARGV_CHARS:
            return CommandIO.usage_error("The command would be %d characters; shorten the inputs.json values to stay under %d." % (SuggestArgv.length(argv), MAX_ARGV_CHARS))
        if self.args.paid:
            guard.mark()
        print("Running in %s:\n%s" % (self.out_dir, invoker.display(argv)), file=sys.stderr)
        code, stdout_path, stderr_path, error = invoker.run(argv, self.out_dir, "dry-run.json" if self.args.dry_run else "result.json")
        CommandIO.print_json(RunReporter.summarize(code, stdout_path, stderr_path, error))
        return code

    def _missing_files(self):
        # Names the first gather output that is missing from the folder, or None.
        for name in ("digest.md", "prompting-rubric.md", "sessions.json", "inputs.json"):
            if not (self.out_dir / name).is_file():
                return "%s not found in %s. Run `gather` first and pass the out_dir it printed." % (name, self.out_dir)
        return None

    def _validated_inputs(self):
        # Validates inputs.json against the tags in sessions.json.
        index = CommandIO.load_json(self.out_dir / "sessions.json", "sessions.json")
        known = {e.get("tag") for key in ("sessions", "prompts") for e in index.get(key, []) if isinstance(e, dict)}
        return PromptingRun.validator(known, Redactor(Path.home())).validate(CommandIO.load_json(self.out_dir / "inputs.json", "inputs.json"))


class ReportCommand:
    """Writes report.md and prints the idea-to-evidence map."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags.
        self.args = args

    def run(self) -> int:
        # Builds the report, or exits 2 when result.json or sessions.json is missing.
        try:
            report = IdeaReporter(Path(self.args.out).expanduser()).build()
        except ReportError as error:
            return CommandIO.usage_error(str(error))
        CommandIO.print_json(report)
        return 0


class FeedbackCommand:
    """Records an explicit accept or reject of one idea with its exact title."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags.
        self.args = args
        self.out_dir = Path(args.out).expanduser()

    def run(self) -> int:
        # Finds the idea, resolves the project and CLI, and runs the feedback command.
        result = CommandIO.load_json(self.out_dir / "result.json", "result.json")
        ideas = ((result.get("data") or {}).get("ideas") or []) if isinstance(result, dict) else []
        match = [i for i in ideas if isinstance(i, dict) and i.get("id") == self.args.idea]
        if not match:
            return CommandIO.usage_error("No idea with id %s in result.json. Ids look like idea-001." % self.args.idea)
        project = self._project()
        if not project or not PROJECT_RE.match(project):
            return CommandIO.usage_error("No suggestion project. Create it first (see the skill's Deliver section), then pass --project or set project in inputs.json.")
        invoker = CliInvoker(self.args.cli)
        exe = invoker.resolve()
        if not exe:
            CommandIO.print_json({"error": NO_CLI})
            return 127
        argv = FeedbackArgv.build(exe, "accept" if self.args.accept else "reject", project, match[0]["title"], self.args.reason or "", Redactor(Path.home()))
        print(invoker.display(argv), file=sys.stderr)
        return subprocess.call(argv)

    def _project(self):
        # --project, else project in inputs.json.
        if self.args.project:
            return self.args.project
        inputs = TextTools.read_json_file(self.out_dir / "inputs.json")
        return inputs.get("project") if isinstance(inputs, dict) else None


class ArgumentParserFactory:
    """Builds the command-line interface."""

    @staticmethod
    def build() -> argparse.ArgumentParser:
        # One subparser per command, with the shared session-selection flags on gather.
        parser = argparse.ArgumentParser(
            prog="prompt_review.py",
            description="Critique how you prompt your coding agents, from your own recent sessions, with the Vidbyte Suggestion Agent.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "Typical flow:\n"
                "  python3 prompt_review.py gather                        # free, local\n"
                "  python3 prompt_review.py suggest --out <out_dir> --dry-run   # free\n"
                "  python3 prompt_review.py suggest --out <out_dir> --paid      # billed by usage; ask the user first\n"
                "  python3 prompt_review.py report --out <out_dir>              # free, local\n"
            ),
        )
        sub = parser.add_subparsers(dest="command", required=True)
        gather = sub.add_parser("gather", help="Read recent sessions and write the digest files (free, local, read-only).")
        CollectOptions.add_arguments(gather)
        gather.add_argument("--max-chars", type=ArgumentParserFactory.max_chars, default=DEFAULT_MAX_CHARS, help="Size limit for digest.md in characters (%d-%d, default %d)." % (MIN_MAX_CHARS, MAX_MAX_CHARS, DEFAULT_MAX_CHARS))
        gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")
        suggest = sub.add_parser("suggest", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run is free; --paid is billed by usage).")
        suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
        suggest.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
        suggest.add_argument("--paid", action="store_true", help="The usage-billed run (capped at %d tokens). Use only after the user agreed to the cost." % MAX_TOTAL_TOKENS)
        suggest.add_argument("--cli", help="Path or name of the vidbyte-cli executable (default: vidbyte-cli on PATH).")
        report = sub.add_parser("report", help="Map each idea to the tagged prompts, signals, and rubric items it cites (free, local).")
        report.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea (free).")
        feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
        verdict = feedback.add_mutually_exclusive_group(required=True)
        verdict.add_argument("--accept", action="store_true", help="The user said they want this idea.")
        verdict.add_argument("--reject", action="store_true", help="The user said no to this idea.")
        feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
        feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json; this skill uses %s)." % PROJECT_KEY)
        feedback.add_argument("--cli", help="Path or name of the vidbyte-cli executable (default: vidbyte-cli on PATH).")
        return parser

    @staticmethod
    def max_chars(value: str) -> int:
        # Accepts a digest size from 20,000 to 100,000 characters.
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a whole number")
        if not MIN_MAX_CHARS <= number <= MAX_MAX_CHARS:
            raise argparse.ArgumentTypeError("must be from %d to %d" % (MIN_MAX_CHARS, MAX_MAX_CHARS))
        return number


COMMANDS = {"gather": GatherCommand, "suggest": SuggestCommand, "report": ReportCommand, "feedback": FeedbackCommand}


def main(argv=None) -> int:
    # Parses the command line and runs one command.
    CommandIO.configure_streams()
    args = ArgumentParserFactory.build().parse_args(argv)
    return COMMANDS[args.command](args).run()


if __name__ == "__main__":
    sys.exit(main())
