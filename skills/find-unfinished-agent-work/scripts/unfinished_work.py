#!/usr/bin/env python3
"""Find coding-agent work that was left unfinished, and turn it into one checklist.

Four subcommands, run in this order by the find-unfinished-agent-work skill:

  gather    Free and local. Reads the most recent sessions (at most 500, no time limit by default)
            of Claude Code, Codex, OpenCode, Gemini CLI, and Qwen Code; flags sessions whose work was
            not finished; drops work finished later (a later session of the same task, a merged pull
            request, the repository as it is now); redacts secrets, emails, and home paths; and writes
            digest.md, inputs.json, the focus file, and sessions.json into a new output folder.
  suggest   Checks inputs.json, then runs `vidbyte-cli --json --no-input agents suggest run ...` from
            inside the output folder. --dry-run is free; --paid is the one billed step and needs the
            user's yes first.
  report    Local, no model call. Maps the paid run's ideas back to sessions and writes checklist.md,
            grouped by project, newest first, with each session's resume command. Needs result.json.
  feedback  Records the user's explicit accept or reject of one idea, copying its exact title from
            result.json into `vidbyte-cli agents suggest feedback`.

Only the Python standard library is used, and Python 3.8 or newer is enough. vidbyte-cli, git, and gh
are started without a shell, so no quoting differs between bash, zsh, PowerShell, and cmd.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sessionkit.collector import CollectOptions, SessionCollector  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402
from sessionkit.runner import CliInvoker, CommandIO, FeedbackArgv, PaidRunGuard, RunReporter, SuggestArgv, MAX_ARGV_CHARS  # noqa: E402

from unfinished.digest import DEFAULT_MAX_CHARS, MAX_MAX_CHARS, MIN_MAX_CHARS, DigestWriter  # noqa: E402
from unfinished.followup import LaterWorkMatcher, ProjectKeys  # noqa: E402
from unfinished.repo_state import RepoStateProbe  # noqa: E402
from unfinished.report import ChecklistReporter, ReportError  # noqa: E402
from unfinished.signals import UnfinishedDetector  # noqa: E402
from unfinished.status import StatusJudge  # noqa: E402
from unfinished.suggest import (  # noqa: E402
    DEFAULT_COUNT, DIGEST_FILE, FOCUS_FILE, INDEX_FILE, INPUTS_FILE, MAX_COUNT, MIN_COUNT, PROJECT_KEY, RESULT_FILE, SuggestConfig,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
FOCUS_SOURCE = SKILL_DIR / "references" / FOCUS_FILE
SETUP_HINT = "vidbyte-cli was not found. Install it (references/setup.md step 1), or pass --cli with its full path."


class GatherCommand:
    """Reads sessions, flags and verifies unfinished work, and writes the four gather files."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed arguments.
        self.args = args

    def run(self) -> int:
        # Collects, detects, matches, probes, judges, writes, and prints the summary; exit 3 when nothing was read.
        problem = self._check_args()
        if problem:
            return CommandIO.usage_error(problem)
        out_dir = self._out_dir()
        if out_dir is None:
            return CommandIO.usage_error("--out %s already holds a digest; pass a new folder so an earlier run's files are not overwritten." % self.args.out)
        result = SessionCollector(CollectOptions.from_args(self.args)).collect()
        if not result.sessions:
            CommandIO.print_json({"error": "No session matched.", "out_dir": str(out_dir), "read": result.summary(Redactor(Path.home()))})
            return 3
        findings = UnfinishedDetector().detect(result.sessions)
        probe = RepoStateProbe(enabled_git=not self.args.no_git, enabled_gh=not self.args.no_gh, max_folders=self.args.max_repo_checks)
        matches = LaterWorkMatcher(ProjectKeys(use_git=not self.args.no_git)).match(findings)
        items = StatusJudge(probe if not self.args.no_git or not self.args.no_gh else None).judge(findings, matches)
        writer = DigestWriter(out_dir, Path.home(), self.args.max_chars, FOCUS_SOURCE)
        try:
            summary = writer.write(items, result, probe.summary())
        except ValueError as error:
            return CommandIO.usage_error(str(error))
        CommandIO.print_json(summary)
        return 0

    def _check_args(self) -> str:
        # Returns a usage problem, or "".
        if not MIN_MAX_CHARS <= self.args.max_chars <= MAX_MAX_CHARS:
            return "--max-chars must be from %d to %d." % (MIN_MAX_CHARS, MAX_MAX_CHARS)
        if not 0 <= self.args.max_repo_checks <= 200:
            return "--max-repo-checks must be from 0 to 200."
        if not FOCUS_SOURCE.is_file():
            return "The focus file is missing: %s. Reinstall the skill." % FOCUS_SOURCE
        return ""

    def _out_dir(self):
        # A new output folder under the system temp folder, or --out when it holds no digest yet; None when it does.
        if self.args.out:
            path = Path(self.args.out).expanduser()
            if (path / DIGEST_FILE).exists():
                return None
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()
        return Path(tempfile.mkdtemp(prefix="unfinished-work-"))


class SuggestCommand:
    """Validates inputs.json and runs the Suggestion Agent once (dry run or paid) from inside the output folder."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed arguments.
        self.args = args

    def run(self) -> int:
        # Validates, refuses a second paid run, runs vidbyte-cli, and prints the run report.
        out_dir = Path(self.args.out).expanduser().resolve()
        if self.args.dry_run == self.args.paid:
            return CommandIO.usage_error("Pass exactly one of --dry-run or --paid.")
        if not MIN_COUNT <= self.args.count <= MAX_COUNT:
            return CommandIO.usage_error("--count must be from %d to %d." % (MIN_COUNT, MAX_COUNT))
        for name in (DIGEST_FILE, FOCUS_FILE):
            if not (out_dir / name).is_file():
                return CommandIO.usage_error("%s is missing from %s. Run `gather` first, or pass the folder it printed as --out." % (name, out_dir))
        inputs, problems = self._validated(out_dir)
        if problems:
            CommandIO.print_json({"error": "inputs.json needs fixing before the run.", "problems": problems})
            return 2
        guard = PaidRunGuard(out_dir)
        if self.args.paid and guard.problem():
            return CommandIO.usage_error(guard.problem())
        exe = CliInvoker(self.args.cli).resolve()
        if exe is None:
            CommandIO.print_json({"error": SETUP_HINT})
            return 127
        argv = SuggestConfig.argv(exe, inputs, self.args.count, self.args.dry_run)
        if SuggestArgv.length(argv) > MAX_ARGV_CHARS:
            return CommandIO.usage_error("The command line would be %d characters; shorten the inputs.json values (Windows refuses very long command lines)." % SuggestArgv.length(argv))
        if self.args.paid:
            guard.mark()
        invoker = CliInvoker(self.args.cli)
        code, stdout_path, stderr_path, error = invoker.run(argv, out_dir, "dry-run.json" if self.args.dry_run else RESULT_FILE)
        report = RunReporter.summarize(code, stdout_path, stderr_path, error)
        report["command"] = invoker.display(argv)
        CommandIO.print_json(report)
        return code

    def _validated(self, out_dir: Path):
        # (inputs, problems) from inputs.json, checked against the tags in sessions.json.
        data = CommandIO.load_json(out_dir / INPUTS_FILE, INPUTS_FILE)
        index = CommandIO.load_json(out_dir / INDEX_FILE, INDEX_FILE)
        known = {e.get("tag") for e in index.get("items") or [] if isinstance(e, dict) and e.get("tag")}
        known |= {p.get("tag") for p in index.get("prompts") or [] if isinstance(p, dict) and p.get("tag")}
        return SuggestConfig.validator(known, Redactor(Path.home())).validate(data)


class ReportCommand:
    """Writes checklist.md from result.json and sessions.json, or from sessions.json alone."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed arguments.
        self.args = args

    def run(self) -> int:
        # Builds the checklist from result.json and sessions.json; exit 2 on a missing or bad file.
        try:
            summary = ChecklistReporter(Path(self.args.out).expanduser().resolve(), self.args.shell).build()
        except ReportError as error:
            return CommandIO.usage_error(str(error))
        CommandIO.print_json(summary)
        return 0


class FeedbackCommand:
    """Records an explicit accept or reject of one idea, by its exact title."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed arguments.
        self.args = args

    def run(self) -> int:
        # Finds the idea in result.json and runs `agents suggest feedback accept|reject`.
        out_dir = Path(self.args.out).expanduser().resolve()
        if self.args.accept == self.args.reject:
            return CommandIO.usage_error("Pass exactly one of --accept or --reject.")
        result = CommandIO.load_json(out_dir / RESULT_FILE, RESULT_FILE)
        ideas = ((result.get("data") or {}).get("ideas") or []) if isinstance(result, dict) else []
        idea = next((i for i in ideas if isinstance(i, dict) and i.get("id") == self.args.idea), None)
        if idea is None or not idea.get("title"):
            return CommandIO.usage_error("No idea with id %s in result.json. Ids look like idea-001." % self.args.idea)
        project = self.args.project or self._project(out_dir)
        exe = CliInvoker(self.args.cli).resolve()
        if exe is None:
            CommandIO.print_json({"error": SETUP_HINT})
            return 127
        verb = "accept" if self.args.accept else "reject"
        argv = FeedbackArgv.build(exe, verb, project, idea["title"], self.args.reason or "", Redactor(Path.home()))
        invoker = CliInvoker(self.args.cli)
        code, stdout_path, _stderr_path, error = invoker.run(argv, out_dir, "feedback-%s.json" % self.args.idea)
        report = {"exit_code": code, "idea": self.args.idea, "project": project, "verb": verb, "command": invoker.display(argv)}
        if code != 0 and isinstance(error, dict):
            report["error"] = {k: error.get(k) for k in ("code", "message", "hint", "retryable") if k in error}
        CommandIO.print_json(report)
        return code

    @staticmethod
    def _project(out_dir: Path) -> str:
        # The project from inputs.json, else this skill's default project key.
        try:
            data = CommandIO.load_json(out_dir / INPUTS_FILE, INPUTS_FILE)
        except SystemExit:
            return PROJECT_KEY
        return (data.get("project") if isinstance(data, dict) else None) or PROJECT_KEY


class Cli:
    """Argument parsing and dispatch."""

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        # Builds the four subcommands and their options.
        parser = argparse.ArgumentParser(prog="unfinished_work.py", description="Find coding-agent work left unfinished and turn it into one checklist.")
        sub = parser.add_subparsers(dest="command")
        gather = sub.add_parser("gather", help="Read sessions, flag and verify unfinished work, write the digest (free, local).")
        CollectOptions.add_arguments(gather, default_max_sessions=500)
        gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Most characters in digest.md (%d-%d, default %d)." % (MIN_MAX_CHARS, MAX_MAX_CHARS, DEFAULT_MAX_CHARS))
        gather.add_argument("--out", default=None, help="Output folder (default: a new folder under the system temp folder).")
        gather.add_argument("--no-git", action="store_true", help="Do not run git in the sessions' folders.")
        gather.add_argument("--no-gh", action="store_true", help="Do not ask the GitHub CLI about pull requests.")
        gather.add_argument("--max-repo-checks", type=int, default=80, help="Most folders to check with git (0-200, default 80).")
        suggest = sub.add_parser("suggest", help="Run the Suggestion Agent once: --dry-run is free, --paid is billed.")
        suggest.add_argument("--out", required=True, help="The output folder gather printed.")
        suggest.add_argument("--dry-run", action="store_true", help="Check the whole request without calling a model (free).")
        suggest.add_argument("--paid", action="store_true", help="Run it for real (usage-billed; only after the user's yes).")
        suggest.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Most checklist items to ask for (%d-%d, default %d)." % (MIN_COUNT, MAX_COUNT, DEFAULT_COUNT))
        suggest.add_argument("--cli", default=None, help="Path to vidbyte-cli when it is not on PATH.")
        report = sub.add_parser("report", help="Write checklist.md from the paid run's result.json (local, no model call).")
        report.add_argument("--out", required=True, help="The output folder gather printed.")
        report.add_argument("--shell", choices=("bash", "powershell"), default=None, help="Spell resume commands for this shell (default: PowerShell on Windows, bash elsewhere).")
        feedback = sub.add_parser("feedback", help="Record an explicit accept or reject of one idea (free, local).")
        feedback.add_argument("--out", required=True, help="The output folder gather printed.")
        feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
        feedback.add_argument("--accept", action="store_true", help="The user accepted the idea.")
        feedback.add_argument("--reject", action="store_true", help="The user rejected the idea.")
        feedback.add_argument("--reason", default=None, help="The user's own words, optional.")
        feedback.add_argument("--project", default=None, help="Suggestion project key (default: inputs.json, else %s)." % PROJECT_KEY)
        feedback.add_argument("--cli", default=None, help="Path to vidbyte-cli when it is not on PATH.")
        return parser

    @staticmethod
    def main(argv=None) -> int:
        # Parses argv and runs one subcommand.
        CommandIO.configure_streams()
        parser = Cli.parser()
        args = parser.parse_args(argv)
        commands = {"gather": GatherCommand, "suggest": SuggestCommand, "report": ReportCommand, "feedback": FeedbackCommand}
        if args.command not in commands:
            parser.print_help()
            return 2
        return commands[args.command](args).run()


if __name__ == "__main__":
    sys.exit(Cli.main())
