#!/usr/bin/env python3
"""Find chores you redo by hand in fresh coding-agent sessions on a cadence, and turn each into a scheduled routine.

Subcommands, in the order the automate-recurring-chores skill uses them:

  gather    Free and local. Reads the most recent interactive sessions (Claude Code, Codex, OpenCode,
            Gemini CLI, Qwen Code, OpenClaude, Factory Droid, Pi) through sessionkit, groups sessions
            that start the same task, measures each group's cadence, inputs, and outputs, and writes
            digest.md (sent), chores.json (local), inputs.json (sent values), and chore-routines.md
            (the focus file, sent) into a new folder.
  suggest   Validates inputs.json and runs `vidbyte-cli --json --no-input agents suggest run` from inside
            the folder. --dry-run is free; --paid is the one usage-billed step and needs the user's yes.
  report    Free and local. Maps each idea in result.json to the chores, runs, and sessions it cites,
            flags gaps, and writes report.md.
  routine   Free and local. After the user's explicit yes for one idea, writes a routine folder (prompt,
            config, runner, schedule commands) where the user chose. It never registers or runs anything.
  feedback  Records the user's explicit accept or reject of one idea with `vidbyte-cli agents suggest feedback`.

Only the Python standard library is used; Python 3.8 or newer is enough. vidbyte-cli and the
harnesses are started without a shell.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chores.cadence import CadenceAnalyzer  # noqa: E402
from chores.detector import ChoreDetector  # noqa: E402
from chores.digest import ChoreDigestWriter, ChoreRenderer, EnvironmentProbe, TagBook  # noqa: E402
from chores.report import RoutineReporter  # noqa: E402
from chores.routine import SCHEDULERS, RoutineError, RoutineWriter  # noqa: E402
from chores.suggest import FeedbackCommand, SuggestCommand  # noqa: E402
from sessionkit.collector import CollectOptions, SessionCollector  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402
from sessionkit.runner import CommandIO  # noqa: E402

FOCUS_FILE = Path(__file__).resolve().parent.parent / "references" / "chore-routines.md"
DEFAULT_MAX_CHARS = 60_000


class GatherCommand:
    """`gather`: collect sessions, detect chores, and write the digest folder."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Holds the parsed options.
        self.args = args
        self.home = Path.home()

    def run(self) -> int:
        # Returns 0 with files written, 2 for bad options, or 3 when there is nothing to send.
        problem = self._check_options()
        if problem:
            return CommandIO.usage_error(problem)
        redactor = Redactor(self.home)
        collected = SessionCollector(CollectOptions.from_args(self.args), self.home).collect()
        summary = collected.summary(redactor)
        if not collected.sessions:
            CommandIO.print_json({"reason": "no_sessions", "read": summary, "hint": "No interactive sessions were found. Check the folders in read[], or pass --claude-dir, --codex-home, --opencode-dir, --gemini-dir, --qwen-dir, --openclaude-dir, --factory-dir, or --pi-dir."})
            return 3
        result = ChoreDetector(min_runs=self.args.min_runs).detect(collected.sessions)
        if not result.chores:
            CommandIO.print_json(self._nothing_found(result, summary, redactor))
            return 3
        out_dir = self._out_dir()
        writer = ChoreDigestWriter(out_dir, redactor, self.args.max_chars, FOCUS_FILE, self.home, self.args.max_chores)
        try:
            report = writer.write(result, summary, EnvironmentProbe().describe())
        except ValueError as error:
            return CommandIO.usage_error(str(error))
        report["read"] = summary
        hint = self._span_hint(result.span_days)
        if hint:
            report["hint"] = hint
        CommandIO.print_json(report)
        return 0

    def _check_options(self) -> str:
        # The first option out of range, or "".
        if not 3 <= self.args.min_runs <= 10:
            return "--min-runs must be 3 to 10."
        if not 1 <= self.args.max_chores <= 10:
            return "--max-chores must be 1 to 10."
        if not 20_000 <= self.args.max_chars <= 100_000:
            return "--max-chars must be 20000 to 100000."
        return ""

    def _out_dir(self) -> Path:
        # --out (created when missing), else a new folder under the system temp folder.
        if self.args.out:
            path = Path(self.args.out).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()
        return Path(tempfile.mkdtemp(prefix="recurring-chores-")).resolve()

    def _nothing_found(self, result, summary: dict, redactor) -> dict:
        # The exit-3 report: the window, why nothing qualified, and the repeats left out (labels only).
        renderer = ChoreRenderer(redactor, TagBook(), {})
        return {
            "reason": "no_chores", "read": summary, "span_days": result.span_days, "detectable_cadences": CadenceAnalyzer.detectable(result.span_days),
            "already_automated": [{"label": renderer.label(c), "runs": len(c.runs), "evidence": c.automation_evidence} for c in result.automated],
            "irregular": [{"label": renderer.label(c), "runs": len(c.runs), "reason": c.cadence.reason} for c in result.irregular[:10]],
            "shared_templates": result.templates, "stats": dict(result.stats),
            "hint": self._span_hint(result.span_days) or "No group of sessions repeated on a steady cadence with at least %d runs." % self.args.min_runs,
        }

    @staticmethod
    def _span_hint(span: int) -> str:
        # Advice when the sessions read cover too few days to show monthly or quarterly chores.
        if span >= 175:
            return ""
        return (
            "The sessions read span %d days, so chores slower than %s cannot show three runs. To look further back, narrow what is read: "
            "--project-dir <folder> for one project, or --no-<harness> for a busy harness (the cap is 500 sessions)."
        ) % (span, "every two weeks" if span < 59 else "monthly")


class RecurringChoresCLI:
    """Builds the argument parser and dispatches to the subcommands."""

    def main(self, argv: list = None) -> int:
        # Parses argv and runs one subcommand; returns its exit code.
        parser = self._parser()
        args = parser.parse_args(argv)
        if args.command == "gather":
            return GatherCommand(args).run()
        if args.command == "suggest":
            return SuggestCommand(args.out, args.dry_run, args.paid, args.cli).run()
        if args.command == "report":
            CommandIO.print_json(RoutineReporter(args.out, Redactor(Path.home())).build())
            return 0
        if args.command == "routine":
            return self._routine(args)
        if args.command == "feedback":
            return FeedbackCommand(args.out, args.idea, args.accept, args.reason, args.project, args.cli).run()
        parser.print_help()
        return 2

    @staticmethod
    def _routine(args: argparse.Namespace) -> int:
        # Writes one routine folder, or exits 2 with the reason.
        try:
            report = RoutineWriter(args.out, Redactor(Path.home())).write(args.idea, Path(args.dest), args.harness, args.scheduler, args.chore, args.time, args.force)
        except RoutineError as error:
            return CommandIO.usage_error(str(error))
        CommandIO.print_json(report)
        return 0

    def _parser(self) -> argparse.ArgumentParser:
        # The full command-line interface.
        parser = argparse.ArgumentParser(
            prog="recurring_chores.py", formatter_class=argparse.RawDescriptionHelpFormatter,
            description="Find chores you redo by hand in fresh coding-agent sessions on a cadence, and ask the Vidbyte Suggestion Agent for one scheduled routine per chore.",
            epilog=(
                "examples:\n"
                "  python3 recurring_chores.py gather                          # newest 500 sessions, every harness\n"
                "  python3 recurring_chores.py gather --project-dir ~/finances # one project, further back\n"
                "  python3 recurring_chores.py suggest --out <out_dir> --dry-run   # free\n"
                "  python3 recurring_chores.py suggest --out <out_dir> --paid      # billed by usage; ask the user first\n"
                "  python3 recurring_chores.py report --out <out_dir>\n"
                "  python3 recurring_chores.py routine --out <out_dir> --idea idea-001 --dest ~/routines/budget --harness claude-code --scheduler task-scheduler\n"
                "  python3 recurring_chores.py feedback --out <out_dir> --idea idea-002 --reject --reason \"I file taxes with an accountant\"\n"
            ),
        )
        sub = parser.add_subparsers(dest="command")
        gather = sub.add_parser("gather", help="Detect recurring chores and write digest.md (free, local).", description="Read recent sessions, detect chores repeated on a cadence, and write digest.md, chores.json, inputs.json, and chore-routines.md into a new folder. Exit 3 means nothing qualified.")
        CollectOptions.add_arguments(gather, default_max_sessions=500)
        gather.add_argument("--min-runs", type=int, default=3, help="Fewest runs (distinct days) a chore needs, 3-10 (default 3).")
        gather.add_argument("--max-chores", type=int, default=10, help="Most chores in the digest, 1-10 (default 10).")
        gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Size limit of digest.md, 20000-100000 characters (default %d)." % DEFAULT_MAX_CHARS)
        gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")
        suggest = sub.add_parser("suggest", help="Validate inputs.json and run vidbyte-cli agents suggest run (--dry-run free, --paid billed).", description="Validate <out>/inputs.json against chores.json, then run `vidbyte-cli --json --no-input agents suggest run` from inside <out>. Saves dry-run.json or result.json and a .stderr.txt file.")
        suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
        suggest.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
        suggest.add_argument("--paid", action="store_true", help="The usage-billed run. Use only after the user agreed to the cost.")
        suggest.add_argument("--cli", help="The vidbyte-cli executable (default: vidbyte-cli on PATH).")
        report = sub.add_parser("report", help="Tie each idea to its chores and runs, and flag gaps (free, local).", description="Read result.json and chores.json, write report.md, and print a JSON summary.")
        report.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        routine = sub.add_parser("routine", help="Write one routine folder for an idea the user said yes to (free, local).", description="Write routine-prompt.md, routine.json, run_routine.py, SCHEDULE.md, and the scheduler file into --dest. Registers nothing and runs nothing.")
        routine.add_argument("--out", required=True, help="The out_dir that holds result.json and chores.json.")
        routine.add_argument("--idea", required=True, help="The idea id, such as idea-001.")
        routine.add_argument("--dest", required=True, help="The folder the user chose for the routine (empty or new).")
        routine.add_argument("--harness", required=True, choices=("claude-code", "codex"), help="The headless agent that runs the routine.")
        routine.add_argument("--scheduler", required=True, choices=SCHEDULERS, help="Which scheduler's commands to write; none for on-demand runs.")
        routine.add_argument("--chore", help="The chore tag, such as C2, when the idea cites several.")
        routine.add_argument("--time", help="Run time HH:MM local (default: the chore's usual start time).")
        routine.add_argument("--force", action="store_true", help="Write into a non-empty --dest, overwriting the routine files.")
        feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea.", description="Copy the exact title of one idea from result.json into `vidbyte-cli --json agents suggest feedback accept|reject`.")
        feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
        verdict = feedback.add_mutually_exclusive_group(required=True)
        verdict.add_argument("--accept", action="store_true", help="The user said they want this routine.")
        verdict.add_argument("--reject", action="store_true", help="The user said no to this routine.")
        feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
        feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json).")
        feedback.add_argument("--cli", help="The vidbyte-cli executable (default: vidbyte-cli on PATH).")
        return parser


if __name__ == "__main__":
    CommandIO.configure_streams()
    try:
        sys.exit(RecurringChoresCLI().main())
    except KeyboardInterrupt:
        sys.exit(130)
