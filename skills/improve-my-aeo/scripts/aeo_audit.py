#!/usr/bin/env python3
"""improve-my-aeo: audit a site for answer engines for free, then ask the Vidbyte Suggestion Agent what to fix first.

Subcommands:
  gather   Fetch the site (or take only questions) and write audit.md, checks.json, evidence.json, inputs.json,
           and copies of aeo-rubric.md and aeo-checklist.md into a new folder. Free; makes HTTP requests only.
  suggest  Validate inputs.json and run `vidbyte-cli agents suggest run` from that folder (--dry-run is free,
           --paid is billed by model usage).
  report   Tie each idea in result.json to the evidence and checklist items it cites. Free and local.
  feedback Record the user's explicit accept or reject of one idea. Free and local.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aeokit.checks import CheckSuite  # noqa: E402
from aeokit.digest import DigestWriter  # noqa: E402
from aeokit.evidence import EvidenceIndex  # noqa: E402
from aeokit.fetch import HttpFetcher  # noqa: E402
from aeokit.probe import SiteProbe  # noqa: E402
from aeokit.report import IdeaReporter, ReportError  # noqa: E402
from aeokit.runner import MAX_ARGV_CHARS, MAX_TOTAL_TOKENS, PROJECT_RE, CliInvoker, CommandIO, FeedbackArgv, PaidRunGuard, Redactor, RunReporter, SuggestArgv  # noqa: E402
from aeokit.suggest import FILES, INSTRUCTIONS, MAX_INSTRUCTION_BYTES, PROJECT_KEY, AeoRun  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
REFERENCES = {"aeo-rubric.md": SKILL_DIR / "references" / "aeo-rubric.md", "aeo-checklist.md": SKILL_DIR / "references" / "aeo-checklist.md"}
GATHER_FILES = ("audit.md", "checks.json", "evidence.json", "inputs.json", "aeo-rubric.md", "aeo-checklist.md")
NO_CLI = "vidbyte-cli was not found on PATH. Install it (references/setup.md step 1) or pass --cli with its full path."
MAX_QUESTIONS = 3


class GatherCommand:
    """Audits a site (or records only questions) and writes the files every later step reads."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags.
        self.args = args

    def run(self) -> int:
        # Validates flags, probes the site, runs the checks, writes the folder, and prints a summary.
        questions = [" ".join(q.split()) for q in (self.args.question or []) if q.strip()]
        if not self.args.url and not questions:
            return CommandIO.usage_error("Pass --url <site>, --question \"<question>\", or both.")
        if len(questions) > MAX_QUESTIONS:
            return CommandIO.usage_error("Pass at most %d --question values; merge the rest." % MAX_QUESTIONS)
        problem = self._reference_problem()
        if problem:
            return CommandIO.usage_error(problem)
        evidence = EvidenceIndex()
        facts, results, fetch_log = None, CheckSuite.not_audited("No site was audited (question-only run)."), []
        if self.args.url:
            try:
                SiteProbe.origin(self.args.url)
            except ValueError as error:
                return CommandIO.usage_error(str(error))
            fetcher = HttpFetcher(timeout=self.args.timeout)
            probe = SiteProbe(fetcher, self.args.max_pages).run(self.args.url)
            facts, fetch_log = probe["facts"], probe["facts"].get("fetch_log", [])
            if self._unreachable(facts):
                CommandIO.print_json({"error": "The site did not answer: robots.txt and the home page both failed.", "home": facts["home"].get("error") or facts["home"].get("status"), "fetches": fetch_log[:5]})
                return 3
            results = CheckSuite(facts, probe["robots"], evidence).run()
        out_dir = Path(self.args.out).expanduser() if self.args.out else Path(tempfile.mkdtemp(prefix="improve-my-aeo-"))
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, source in REFERENCES.items():
            shutil.copyfile(str(source), str(out_dir / name))
        written = DigestWriter(out_dir, facts, results, evidence, questions).write()
        CommandIO.print_json(self._summary(out_dir, facts, results, fetch_log, written, questions))
        return 0

    @staticmethod
    def _reference_problem():
        # Names a missing reference file or a rubric over the CLI's instructions limit.
        for name, path in REFERENCES.items():
            if not path.is_file():
                return "%s is missing from the skill folder (%s). Reinstall the skill." % (name, path)
        size = REFERENCES["aeo-rubric.md"].stat().st_size
        if size > MAX_INSTRUCTION_BYTES:
            return "aeo-rubric.md is %d bytes; vidbyte-cli accepts at most %d bytes of instructions." % (size, MAX_INSTRUCTION_BYTES)
        return None

    @staticmethod
    def _unreachable(facts: dict) -> bool:
        # True when neither robots.txt nor the home page got any HTTP answer, or both answered 5xx.
        robots, home = facts.get("robots", {}).get("status") or 0, facts.get("home", {}).get("status") or 0
        return (robots == 0 or robots >= 500) and (home == 0 or home >= 500)

    @staticmethod
    def _summary(out_dir: Path, facts, results: list, fetch_log: list, written: dict, questions: list) -> dict:
        # The JSON summary the host agent reads after gather.
        by_status = {}
        for r in results:
            by_status.setdefault(r.status, []).append(r.id)
        return {
            "out_dir": str(out_dir),
            "files": {name: str(out_dir / name) for name in GATHER_FILES},
            "site": facts.get("origin") if facts else None,
            "pages_sampled": len(facts.get("pages", [])) if facts else 0,
            "sitemap_urls": len(facts.get("sitemap_urls", [])) if facts else 0,
            "checks": {"counts": {status: len(ids) for status, ids in by_status.items()}, "fail": by_status.get("fail", []), "warn": by_status.get("warn", [])},
            "requests": len(fetch_log),
            "failed_requests": [row for row in fetch_log if not row.get("status")][:10],
            "audit_chars": written["audit_chars"],
            "questions": questions,
            "next": "Read audit.md and inputs.json, then: aeo_audit.py suggest --out <out_dir> --dry-run",
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
        if (self.out_dir / INSTRUCTIONS[0]).stat().st_size > MAX_INSTRUCTION_BYTES:
            return CommandIO.usage_error("%s is over %d bytes, the CLI's instructions limit." % (INSTRUCTIONS[0], MAX_INSTRUCTION_BYTES))
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
        argv = AeoRun.argv(exe, inputs, self.args.dry_run)
        if SuggestArgv.length(argv) > MAX_ARGV_CHARS:
            return CommandIO.usage_error("The command would be %d characters; shorten the inputs.json values to stay under %d." % (SuggestArgv.length(argv), MAX_ARGV_CHARS))
        if self.args.paid:
            guard.mark()
        print("Running in %s:\n%s" % (self.out_dir, invoker.display(argv)), file=sys.stderr)
        code, stdout_path, stderr_path, error = invoker.run(argv, self.out_dir, "dry-run.json" if self.args.dry_run else "result.json")
        CommandIO.print_json(RunReporter.summarize(code, stdout_path, stderr_path, error))
        return code

    def _missing_files(self):
        # Names the first gather output missing from the folder, or None.
        for name in ("inputs.json", "evidence.json") + FILES + INSTRUCTIONS:
            if not (self.out_dir / name).is_file():
                return "%s not found in %s. Run `gather` first and pass the out_dir it printed." % (name, self.out_dir)
        return None

    def _validated_inputs(self):
        # Validates inputs.json against evidence.json's tags and the checklist's item ids.
        evidence = CommandIO.load_json(self.out_dir / "evidence.json", "evidence.json")
        known = set(evidence) | AeoRun.checklist_ids(self.out_dir / "aeo-checklist.md")
        return AeoRun.validator(known, Redactor()).validate(CommandIO.load_json(self.out_dir / "inputs.json", "inputs.json"))


class ReportCommand:
    """Writes report.md and prints the idea-to-evidence map."""

    def __init__(self, args: argparse.Namespace) -> None:
        # Keeps the parsed flags.
        self.args = args

    def run(self) -> int:
        # Builds the report, or exits 2 when an input file is missing.
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
            return CommandIO.usage_error("No suggestion project. Create it first (see the skill's Deliver section), then pass --project %s or set project in inputs.json." % PROJECT_KEY)
        invoker = CliInvoker(self.args.cli)
        exe = invoker.resolve()
        if not exe:
            CommandIO.print_json({"error": NO_CLI})
            return 127
        argv = FeedbackArgv.build(exe, "accept" if self.args.accept else "reject", project, match[0]["title"], self.args.reason or "", Redactor())
        print(invoker.display(argv), file=sys.stderr)
        return subprocess.call(argv)

    def _project(self):
        # --project, else project in inputs.json.
        if self.args.project:
            return self.args.project
        try:
            inputs = CommandIO.load_json(self.out_dir / "inputs.json", "inputs.json")
        except SystemExit:
            return None
        return inputs.get("project") if isinstance(inputs, dict) else None


class ArgumentParserFactory:
    """Builds the command-line interface."""

    @staticmethod
    def build() -> argparse.ArgumentParser:
        # One subparser per command.
        parser = argparse.ArgumentParser(
            prog="aeo_audit.py",
            description="Audit a site for answer engines and AI agents, then ask the Vidbyte Suggestion Agent what to fix first.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=(
                "Typical flow:\n"
                "  python3 aeo_audit.py gather --url https://example.com          # free\n"
                "  python3 aeo_audit.py suggest --out <out_dir> --dry-run         # free\n"
                "  python3 aeo_audit.py suggest --out <out_dir> --paid            # billed by usage; ask the user first\n"
                "  python3 aeo_audit.py report --out <out_dir>                    # free, local\n"
            ),
        )
        sub = parser.add_subparsers(dest="command", required=True)
        gather = sub.add_parser("gather", help="Audit a site and/or record AEO questions; write the run folder (free).")
        gather.add_argument("--url", help="The site to audit, such as https://example.com (only its origin is used).")
        gather.add_argument("--question", action="append", help="One AEO question in the user's words (repeat up to %d times)." % MAX_QUESTIONS)
        gather.add_argument("--max-pages", type=ArgumentParserFactory.pages, default=25, help="HTML pages to sample, home page included (1-60, default 25).")
        gather.add_argument("--timeout", type=float, default=15.0, help="Seconds to wait for each request (default 15).")
        gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")
        suggest = sub.add_parser("suggest", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run is free; --paid is billed by usage).")
        suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
        suggest.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
        suggest.add_argument("--paid", action="store_true", help="The usage-billed run (capped at %d tokens). Use only after the user agreed to the cost." % MAX_TOTAL_TOKENS)
        suggest.add_argument("--cli", help="Path or name of the vidbyte-cli executable (default: vidbyte-cli on PATH).")
        report = sub.add_parser("report", help="Map each idea to the evidence and checklist items it cites (free, local).")
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
    def pages(value: str) -> int:
        # argparse type for --max-pages: an integer from 1 to 60.
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a whole number")
        if not 1 <= number <= 60:
            raise argparse.ArgumentTypeError("must be from 1 to 60")
        return number


def main(argv=None) -> int:
    # Parses the command line and runs one subcommand.
    CommandIO.configure_streams()
    args = ArgumentParserFactory.build().parse_args(argv)
    commands = {"gather": GatherCommand, "suggest": SuggestCommand, "report": ReportCommand, "feedback": FeedbackCommand}
    return commands[args.command](args).run()


if __name__ == "__main__":
    sys.exit(main())
