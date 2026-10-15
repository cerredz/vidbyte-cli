#!/usr/bin/env python3
"""Turn the corrections you gave your coding agents into proposed agent skills.

Subcommands (each prints one JSON document on stdout):
  gather    Read recent sessions, find the corrections, cluster them into scenarios, write the digest (free, local).
  suggest   Check inputs.json and run `vidbyte-cli agents suggest run` (--dry-run is free; --paid is billed by usage).
  report    Map each proposed skill to the corrections behind it and write report.md (free, local).
  scaffold  Draft a SKILL.md from one proposed skill; --write --dest writes it, never over an existing file (free, local).
  feedback  Record the user's explicit accept or reject of one idea (free, local).

Exit codes: 0 ok; 2 usage or validation; 3 nothing to send (gather); 127 no vidbyte-cli; any other code is vidbyte-cli's.
Only the Python 3.8+ standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from correctionkit.detector import CorrectionDetector  # noqa: E402
from correctionkit.digest import MAX_CHARS, MIN_CHARS, DigestTooSmall, DigestWriter  # noqa: E402
from correctionkit.inventory import SkillInventory  # noqa: E402
from correctionkit.report import IdeaReporter  # noqa: E402
from correctionkit.scaffold import ScaffoldRefusal, SkillScaffolder  # noqa: E402
from correctionkit.scenarios import MistakeKinds, ScenarioBuilder  # noqa: E402
from correctionkit.suggest import CorrectionSuggestConfig  # noqa: E402
from sessionkit.collector import CollectOptions, SessionCollector  # noqa: E402
from sessionkit.model import TextTools  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402
from sessionkit.runner import MAX_ARGV_CHARS, PROJECT_RE, CliInvoker, CommandIO, FeedbackArgv, PaidRunGuard, RunReporter, SuggestArgv  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
FOCUS_FILE = SKILL_DIR / "references" / CorrectionSuggestConfig.FOCUS
NO_CLI = "vidbyte-cli is not on PATH. Follow references/setup.md step 1, or pass --cli with its full path."
DEFAULT_MAX_CHARS = 60_000
DEFAULT_SCENARIOS = 12
MAX_SCENARIOS = 20


class OutFolder:
    """Creates and checks the output folder every subcommand works in."""

    @staticmethod
    def create(path) -> tuple:
        # Returns (folder, created_by_us): a new temp folder, or --out when it does not exist or is empty.
        if not path:
            return Path(tempfile.mkdtemp(prefix="turn-corrections-")), True
        folder = Path(path).expanduser().resolve()
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise SystemExit(CommandIO.usage_error("--out %s already exists and is not an empty folder; pass a new folder or leave --out out." % folder))
        folder.mkdir(parents=True, exist_ok=True)
        return folder, False

    @staticmethod
    def existing(path: str, *names: str) -> Path:
        # Returns --out as a Path after checking it holds the files a later subcommand needs.
        folder = Path(path).expanduser().resolve()
        missing = [n for n in names if not (folder / n).is_file()]
        if missing:
            raise SystemExit(CommandIO.usage_error("%s has no %s. Run `gather` first, or pass the out_dir it printed as --out." % (folder, ", ".join(missing))))
        return folder


class GatherCommand:
    """Reads sessions, finds corrections, builds scenarios, and writes the digest and its companions."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags and this machine's home folder for the reader and the redactor.
        self.args = args
        self.home = Path.home()
        self.redact = Redactor(self.home)

    def run(self) -> int:
        # Collects, detects, clusters, inventories, and writes; exits 3 when there is nothing to send.
        if not FOCUS_FILE.is_file():
            return CommandIO.usage_error("The focus file %s is missing; reinstall the skill." % FOCUS_FILE)
        collector = SessionCollector(CollectOptions.from_args(self.args), self.home)
        result = collector.collect()
        summary = result.summary(self.redact)
        if not result.sessions:
            return self.nothing("no_sessions", summary, None)
        detector = CorrectionDetector()
        episodes = detector.detect(result.sessions)
        overall = detector.overall(result.sessions, episodes)
        if not episodes:
            return self.nothing("no_corrections", summary, overall)
        scenarios = ScenarioBuilder().build(episodes)
        skills = self.inventory(collector, scenarios)
        out_dir, created = OutFolder.create(self.args.out)
        try:
            written = DigestWriter(out_dir, self.redact, self.args.max_chars, self.args.max_scenarios, FOCUS_FILE).write(scenarios, overall, skills, summary)
        except (DigestTooSmall, ValueError) as error:
            if created:
                shutil.rmtree(str(out_dir), ignore_errors=True)
            return CommandIO.usage_error(str(error))
        CommandIO.print_json(self.report(out_dir, summary, overall, scenarios, written, skills))
        return 0

    def inventory(self, collector: SessionCollector, scenarios: list) -> list:
        # Existing skills from the Claude and OpenClaude folders read, ~/.agents, the Codex home, and the top scenarios' project folders.
        claude = collector.readers.get("claude")
        openclaude = collector.readers.get("openclaude")
        codex = collector.readers.get("codex")
        claude_dirs = (list(claude.roots) if claude else []) + (list(openclaude.roots) if openclaude else [])
        codex_home = codex.roots[0] if codex else None
        folders = Counter()
        for scenario in scenarios[: self.args.max_scenarios]:
            for episode in scenario.episodes:
                if episode.session.cwd:
                    folders[episode.session.cwd] += 1
        return SkillInventory(self.home, claude_dirs, codex_home).scan([f for f, _n in folders.most_common()])

    def nothing(self, reason: str, summary: dict, overall) -> int:
        # Prints why nothing will be sent (no sessions, or no corrections in them) and returns exit 3.
        hint = {
            "no_sessions": "No interactive session matched. Check `read` and `skipped`, and offer a wider --days or --since, another --claude-dir, or no --project-dir.",
            "no_corrections": "No prompt in these sessions corrected the agent, so there is nothing to turn into a skill. Offer more sessions (no --days, another --claude-dir) or stop here.",
        }[reason]
        CommandIO.print_json({"out_dir": None, "reason": reason, "hint": hint, "sessions": summary, "corrections": overall})
        return 3

    def report(self, out_dir: Path, summary: dict, overall: dict, scenarios: list, written: dict, skills: list) -> dict:
        # The JSON summary the agent reads: where the files are, what was read, the corrections, and the scenario table.
        full = set(written["full"])
        table = [{
            "tag": s.tag, "kind": s.kind, "kind_label": MistakeKinds.LABELS[s.kind], "label": self.redact(s.label()), "full": s.tag in full,
            "corrections": s.corrections, "episodes": len(s.episodes), "sessions": s.sessions, "projects": len(s.projects),
            "prompts_lost": s.prompts_lost, "tool_calls_redone": s.tool_calls_redone, "harnesses": dict(s.harnesses.most_common()),
        } for s in scenarios]
        return {
            "out_dir": str(out_dir),
            "files": {name: str(out_dir / name) for name in ("digest.md", "corrections.json", "inputs.json", FOCUS_FILE.name)},
            "digest_chars": written["digest_chars"],
            "focus_bytes": written["focus_bytes"],
            "count": written["count"],
            "sessions": summary,
            "corrections": overall,
            "scenarios": table,
            "tags": written["tags"],
            "skills_found": [{"name": self.redact(e["name"]), "scope": self.redact(e["scope"])} for e in skills],
            "redactions_in_digest": dict(self.redact.counts),
        }


class SuggestCommand:
    """Validates inputs.json and runs the Suggestion Agent, free with --dry-run or usage-billed with --paid."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the flags and the skill's suggest configuration.
        self.args = args
        self.config = CorrectionSuggestConfig()
        self.redact = Redactor(Path.home())

    def run(self) -> int:
        # Checks the inputs and the argv, guards the paid run, runs vidbyte-cli inside out_dir, and prints the report.
        out_dir = OutFolder.existing(self.args.out, "digest.md", "corrections.json", "inputs.json", self.config.FOCUS)
        index = CommandIO.load_json(out_dir / "corrections.json", "corrections.json")
        inputs, problems = self.config.validator(index, self.redact).validate(CommandIO.load_json(out_dir / "inputs.json", "inputs.json"))
        if problems:
            CommandIO.print_json({"error": "inputs.json needs fixing", "problems": problems})
            for problem in problems:
                print("inputs.json: " + problem, file=sys.stderr)
            return 2
        invoker = CliInvoker(self.args.cli)
        exe = invoker.resolve()
        if not exe:
            CommandIO.print_json({"error": ("--cli %s was not found. " % self.args.cli if self.args.cli else "") + NO_CLI})
            return 127
        argv = self.config.argv(exe, inputs, index, self.args.dry_run)
        if SuggestArgv.length(argv) > MAX_ARGV_CHARS:
            return CommandIO.usage_error("The command line would be %d characters; the limit is %d. Shorten the inputs.json values." % (SuggestArgv.length(argv), MAX_ARGV_CHARS))
        if self.args.paid:
            guard = PaidRunGuard(out_dir)
            problem = guard.problem()
            if problem:
                return CommandIO.usage_error(problem)
            guard.mark()
        print(invoker.display(argv), file=sys.stderr)
        code, stdout_path, stderr_path, error = invoker.run(argv, out_dir, "result.json" if self.args.paid else "dry-run.json")
        report = RunReporter.summarize(code, stdout_path, stderr_path, error)
        report.update({"mode": "paid" if self.args.paid else "dry_run", "count": self.config.count(index)})
        CommandIO.print_json(report)
        return code


class ReportCommand:
    """Maps each proposed skill to its evidence and writes report.md."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the flags.
        self.args = args

    def run(self) -> int:
        # Builds and prints the report.
        out_dir = OutFolder.existing(self.args.out, "corrections.json", "result.json")
        CommandIO.print_json(IdeaReporter(out_dir, Redactor(Path.home())).build())
        return 0


class ScaffoldCommand:
    """Drafts SKILL.md for one idea and writes it only with --write --dest."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the flags.
        self.args = args

    def run(self) -> int:
        # Renders the draft, saves it in out_dir, and writes it to --dest only when --write is given.
        if bool(self.args.write) != bool(self.args.dest):
            return CommandIO.usage_error("--write and --dest go together: --dest names the skills folder, --write confirms the user said yes.")
        out_dir = OutFolder.existing(self.args.out, "corrections.json", "result.json")
        try:
            scaffolder = SkillScaffolder(out_dir, Redactor(Path.home()))
            rendered = scaffolder.render(self.args.idea, self.args.slug, self.args.description)
            if self.args.write:
                target = scaffolder.write(rendered, Path(self.args.dest))
                CommandIO.print_json({"written": str(target), "idea": rendered["idea"], "slug": rendered["slug"], "draft_path": rendered["draft_path"]})
                return 0
        except ScaffoldRefusal as refusal:
            return CommandIO.usage_error(str(refusal))
        rendered["destinations"] = self.destinations(out_dir, rendered["slug"])
        CommandIO.print_json(rendered)
        return 0

    def destinations(self, out_dir: Path, slug: str) -> list:
        # The usual skills folders for this slug: the main project's .claude/skills and .agents/skills, then the personal ones.
        index = CommandIO.load_json(out_dir / "corrections.json", "corrections.json")
        cwds = Counter(s.get("cwd") for s in index.get("sessions", []) if s.get("cwd"))
        home = Path.home()
        found = []
        for cwd, _n in cwds.most_common(2):
            found.append({"dest": str(Path(cwd) / ".claude" / "skills"), "loaded_by": "Claude Code in this repository", "path": str(Path(cwd) / ".claude" / "skills" / slug / "SKILL.md")})
            found.append({"dest": str(Path(cwd) / ".agents" / "skills"), "loaded_by": "Codex, OpenCode, and other agents in this repository", "path": str(Path(cwd) / ".agents" / "skills" / slug / "SKILL.md")})
        found.append({"dest": str(home / ".claude" / "skills"), "loaded_by": "Claude Code in every project", "path": str(home / ".claude" / "skills" / slug / "SKILL.md")})
        found.append({"dest": str(home / ".agents" / "skills"), "loaded_by": "Codex and other agents in every project", "path": str(home / ".agents" / "skills" / slug / "SKILL.md")})
        for item in found:
            item["exists"] = Path(item["path"]).exists()
        return found


class FeedbackCommand:
    """Records an explicit accept or reject with the idea's exact title."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the flags.
        self.args = args

    def run(self) -> int:
        # Finds the idea's title, picks the project, and runs vidbyte-cli agents suggest feedback.
        out_dir = OutFolder.existing(self.args.out, "result.json")
        document = CommandIO.load_json(out_dir / "result.json", "result.json")
        ideas = [i for i in ((document.get("data") or {}).get("ideas") or []) if isinstance(i, dict)] if isinstance(document, dict) else []
        match = [i for i in ideas if str(i.get("id")) == self.args.idea]
        if not match:
            return CommandIO.usage_error("No idea %s in result.json; the ids are: %s." % (self.args.idea, ", ".join(str(i.get("id")) for i in ideas) or "none"))
        project = self.project(out_dir)
        if not PROJECT_RE.match(project):
            return CommandIO.usage_error("%r is not a valid suggestion project key." % project)
        invoker = CliInvoker(self.args.cli)
        exe = invoker.resolve()
        if not exe:
            CommandIO.print_json({"error": ("--cli %s was not found. " % self.args.cli if self.args.cli else "") + NO_CLI})
            return 127
        verb = "accept" if self.args.accept else "reject"
        argv = FeedbackArgv.build(exe, verb, project, str(match[0].get("title") or ""), self.args.reason, Redactor(Path.home()))
        print(invoker.display(argv), file=sys.stderr)
        code, stdout_path, stderr_path, error = invoker.run(argv, out_dir, "feedback-%s-%s.json" % (self.args.idea, verb))
        CommandIO.print_json({"exit_code": code, "idea": self.args.idea, "verdict": verb, "project": project,
                              "output_file": str(stdout_path) if stdout_path.exists() else None, "stderr_file": str(stderr_path), "error": error})
        return code

    def project(self, out_dir: Path) -> str:
        # --project, else inputs.json's project, else this skill's default project key.
        if self.args.project:
            return self.args.project
        inputs_path = out_dir / "inputs.json"
        if inputs_path.is_file():
            inputs = TextTools.read_json_file(inputs_path)
            if isinstance(inputs, dict) and inputs.get("project"):
                return str(inputs["project"])
        return CorrectionSuggestConfig.PROJECT


class CommandLine:
    """Builds the argument parser and dispatches to the command classes."""

    COMMANDS = {"gather": GatherCommand, "suggest": SuggestCommand, "report": ReportCommand, "scaffold": ScaffoldCommand, "feedback": FeedbackCommand}

    def parser(self) -> argparse.ArgumentParser:
        # The full parser with one subparser per command.
        parser = argparse.ArgumentParser(prog="corrections.py", description="Turn the corrections you gave your coding agents into proposed agent skills.")
        sub = parser.add_subparsers(dest="command")
        self.add_gather(sub)
        self.add_suggest(sub)
        report = sub.add_parser("report", help="Map each proposed skill to its corrections and write report.md (free, local).")
        report.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        self.add_scaffold(sub)
        self.add_feedback(sub)
        return parser

    @staticmethod
    def add_gather(sub) -> None:
        # The gather subcommand: sessionkit's session flags plus scenario, size, and output options.
        gather = sub.add_parser("gather", help="Find corrections in recent sessions and write the digest (free, local).",
                                description="Read up to 500 recent sessions, find the prompts that corrected the agent, cluster them into scenarios, and write digest.md, corrections.json, inputs.json, and the focus file into a new folder. Exit 3 means nothing to send.")
        CollectOptions.add_arguments(gather, default_max_sessions=500, default_days=None)
        gather.add_argument("--max-scenarios", type=IntRange(1, MAX_SCENARIOS, "--max-scenarios"), default=DEFAULT_SCENARIOS, help="Scenarios shown in full in the digest (1-%d, default %d)." % (MAX_SCENARIOS, DEFAULT_SCENARIOS))
        gather.add_argument("--max-chars", type=IntRange(MIN_CHARS, MAX_CHARS, "--max-chars"), default=DEFAULT_MAX_CHARS, help="Size limit of digest.md (%d-%d, default %d)." % (MIN_CHARS, MAX_CHARS, DEFAULT_MAX_CHARS))
        gather.add_argument("--out", help="Output folder; must not exist or be empty (default: a new folder under the system temp folder).")

    @staticmethod
    def add_suggest(sub) -> None:
        # The suggest subcommand: one of --dry-run or --paid, and an optional --cli.
        suggest = sub.add_parser("suggest", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run free, --paid usage-billed).")
        suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
        mode = suggest.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
        mode.add_argument("--paid", action="store_true", help="The usage-billed run. Use only after the user agreed to the cost.")
        suggest.add_argument("--cli", help="The vidbyte-cli executable to run (default: vidbyte-cli on PATH).")

    @staticmethod
    def add_scaffold(sub) -> None:
        # The scaffold subcommand: an idea id, optional overrides, and the guarded write.
        scaffold = sub.add_parser("scaffold", help="Draft SKILL.md from one idea; write it only with --write --dest (free, local).")
        scaffold.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        scaffold.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
        scaffold.add_argument("--slug", help="Use this skill name instead of the idea's.")
        scaffold.add_argument("--description", help="Use this 'Use when ...' description instead of the idea's.")
        scaffold.add_argument("--dest", help="The skills folder to write into, such as .claude/skills or ~/.agents/skills.")
        scaffold.add_argument("--write", action="store_true", help="Write <dest>/<slug>/SKILL.md. Only after the user said yes; never overwrites.")

    @staticmethod
    def add_feedback(sub) -> None:
        # The feedback subcommand: an idea id, a verdict, and optional reason, project, and CLI.
        feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea (free, local).")
        feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
        feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
        verdict = feedback.add_mutually_exclusive_group(required=True)
        verdict.add_argument("--accept", action="store_true", help="The user said they want this skill.")
        verdict.add_argument("--reject", action="store_true", help="The user said no to this skill.")
        feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
        feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json, else %s)." % CorrectionSuggestConfig.PROJECT)
        feedback.add_argument("--cli", help="The vidbyte-cli executable to run (default: vidbyte-cli on PATH).")

    def main(self, argv=None) -> int:
        # Parses argv and runs one command; an unexpected error prints a JSON error without transcript text.
        parser = self.parser()
        args = parser.parse_args(argv)
        if args.command not in self.COMMANDS:
            parser.print_help(sys.stderr)
            return 2
        try:
            return self.COMMANDS[args.command](args).run()
        except SystemExit:
            raise
        except KeyboardInterrupt:
            return 130
        except Exception as error:  # Never a traceback that could echo transcript text.
            CommandIO.print_json({"error": {"type": type(error).__name__, "where": self.location(error), "message": "corrections.py %s failed unexpectedly." % args.command}})
            return 1

    @staticmethod
    def location(error: Exception) -> str:
        # The file and line where the error was raised, without the message (which may quote a transcript).
        trace = error.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        return "%s:%d" % (os.path.basename(trace.tb_frame.f_code.co_filename), trace.tb_lineno) if trace else "?"


class IntRange:
    """An argparse type for an integer inside a closed range."""

    def __init__(self, low: int, high: int, name: str) -> None:
        # Remembers the bounds and the flag name for the error message.
        self.low, self.high, self.name = low, high, name

    def __call__(self, value: str) -> int:
        # Parses value, raising argparse's error (exit 2) when it is not an integer in range.
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("%s must be a whole number." % self.name)
        if not self.low <= number <= self.high:
            raise argparse.ArgumentTypeError("%s must be from %d to %d." % (self.name, self.low, self.high))
        return number


if __name__ == "__main__":
    CommandIO.configure_streams()
    sys.exit(CommandLine().main())
