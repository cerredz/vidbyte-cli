"""Usage-billed `agents suggest run` plumbing: inputs validation, argv, the paid-run guard, and the CLI call."""

from __future__ import annotations

import datetime as dt
import json
import re
import shlex
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

from .model import TextTools

MODEL = "gpt-6-sol"
MAX_TOTAL_TOKENS = 400_000
RATES_PER_MILLION = {"input": 2.0, "cached_input": 0.2, "output": 10.0}
MARKUP = 1.02
USAGE_KEYS = ("model_calls", "input_tokens", "cached_input_tokens", "output_tokens")
MAX_ARGV_CHARS = 24_000  # Windows refuses command lines over 32,767 characters.
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CommandIO:
    """Shared console behavior: UTF-8 streams, JSON errors on stdout, and readable input files."""

    @staticmethod
    def configure_streams() -> None:
        # Makes stdout and stderr UTF-8 so redacted text with any characters prints on Windows consoles.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    @staticmethod
    def usage_error(message: str) -> int:
        # Prints {"error": message} on stdout and the message on stderr; returns exit code 2.
        print(json.dumps({"error": message}))
        print("Error: " + message, file=sys.stderr)
        return 2

    @staticmethod
    def load_json(path: Path, label: str):
        # Loads a JSON file the skill wrote or the agent edited; exits 2 with a fix when it is missing or invalid.
        try:
            with open(str(path), encoding="utf-8-sig") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise SystemExit(CommandIO.usage_error("%s not found: %s. Run `gather` first, or pass the folder it printed as --out." % (label, path)))
        except ValueError as error:
            raise SystemExit(CommandIO.usage_error("%s is not valid JSON (%s): %s" % (label, error, path)))

    @staticmethod
    def print_json(document: object) -> None:
        # Prints a JSON document with two-space indentation.
        print(json.dumps(document, indent=2, ensure_ascii=False))


class InputsValidator:
    """Checks inputs.json (goal, project, and typed flag values) before any CLI call."""

    def __init__(self, flag_kinds: "OrderedDict", tagged_kinds: set, tag_pattern: str, known_tags: set, redactor, max_goal_chars: int = 700, max_value_chars: int = 500, required_any: tuple = ()) -> None:
        # flag_kinds maps an inputs.json key to (CLI flag, most values); tag_pattern's group(1) holds the tag list.
        self.flag_kinds = flag_kinds
        self.tagged_kinds = set(tagged_kinds)
        self.tag_re = re.compile(tag_pattern)
        self.known_tags = set(known_tags)
        self.redact = redactor
        self.max_goal_chars = max_goal_chars
        self.max_value_chars = max_value_chars
        self.required_any = tuple(required_any)

    def validate(self, data: object):
        # Returns (inputs, problems); inputs = {"goal", "project", "values": OrderedDict[kind, list[str]]}.
        if not isinstance(data, dict):
            return None, ["inputs.json must hold one JSON object."]
        problems = self._unknown_keys(data)
        goal = self._goal(data.get("goal"), problems)
        project = self._project(data.get("project"), problems)
        values: "OrderedDict[str, list]" = OrderedDict()
        for kind, (flag, most) in self.flag_kinds.items():
            cleaned = self._values(kind, data.get(kind), most, problems)
            if cleaned is not None:
                values[kind] = cleaned
        if self.required_any and not any(values.get(k) for k in self.required_any):
            problems.append("Write at least one %s value from the digest before running." % " or ".join(self.required_any))
        return {"goal": goal, "project": project, "values": values}, problems

    def _unknown_keys(self, data: dict) -> list:
        # Lists keys that are neither goal, project, nor a flag kind.
        allowed = {"goal", "project"} | set(self.flag_kinds)
        return ["Unknown key %r. Allowed keys: goal, project, %s." % (k, ", ".join(self.flag_kinds)) for k in data if k not in allowed]

    def _goal(self, goal: object, problems: list) -> str:
        # Requires a non-empty goal under the length limit; returns it redacted and on one line.
        if not isinstance(goal, str) or not goal.strip():
            problems.append("goal must be a non-empty string.")
            return ""
        goal = self.redact(" ".join(goal.split()))
        if len(goal) > self.max_goal_chars:
            problems.append("goal has %d characters; keep it under %d." % (len(goal), self.max_goal_chars))
        return goal

    def _project(self, project: object, problems: list):
        # Accepts null or a suggestion project key.
        if project is not None and (not isinstance(project, str) or not PROJECT_RE.match(project)):
            problems.append("project must be null or a key of 1-64 lowercase letters, digits, hyphens, or underscores.")
        return project

    def _values(self, kind: str, items: object, most: int, problems: list):
        # Cleans one kind's list: strings only, at most `most`, each under the limit, tagged when required.
        if items is None:
            items = []
        if not isinstance(items, list) or any(not isinstance(i, str) for i in items):
            problems.append("%s must be a list of strings." % kind)
            return None
        cleaned = [self.redact(" ".join(i.split())) for i in items if i.strip()]
        if len(cleaned) > most:
            problems.append("%s has %d values; this skill passes at most %d. Merge or drop the weakest." % (kind, len(cleaned), most))
        for value in cleaned:
            if len(value) > self.max_value_chars:
                problems.append("A %s value has %d characters; keep each value under %d: %s" % (kind, len(value), self.max_value_chars, TextTools.one_line(value, 80)))
            if kind in self.tagged_kinds:
                self._check_tags(kind, value, problems)
        return cleaned

    def _check_tags(self, kind: str, value: str, problems: list) -> None:
        # Requires a leading tag such as [P3] or [all], and that every tag exists in the digest.
        match = self.tag_re.match(value)
        if not match:
            problems.append("Every %s value must start with a bracketed digest tag, for example [S2], [P1 P4], or [all]: %s" % (kind, TextTools.one_line(value, 80)))
            return
        for tag in re.split(r"[ ,]+", match.group(1).strip()):
            if tag and tag != "all" and self.known_tags and tag not in self.known_tags:
                problems.append("%s names %s, which is not in the digest index: %s" % (kind, tag, TextTools.one_line(value, 80)))


class SuggestArgv:
    """Builds the exact `agents suggest run` command line."""

    @staticmethod
    def build(exe: str, goal: str, categories, count: int, flag_kinds: "OrderedDict", values: dict, files, instructions, project, dry_run: bool, horizon: str = "any") -> list:
        # Root options first, then the run options in a fixed order; --max-total-tokens is always set.
        argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", goal]
        for category in categories:
            argv += ["--category", category]
        argv += ["--count", str(count), "--horizon", horizon]
        for kind, (flag, _most) in flag_kinds.items():
            for value in values.get(kind, []):
                argv += [flag, value]
        for name in files or ():
            argv += ["--files", name]
        for name in instructions or ():
            argv += ["--instructions", name]
        if project:
            argv += ["--project", project]
        argv += ["--max-total-tokens", str(MAX_TOTAL_TOKENS)]
        if dry_run:
            argv.append("--dry-run")
        return argv

    @staticmethod
    def length(argv: list) -> int:
        # Approximates the command-line length Windows sees, including quoting.
        return sum(len(a) + 3 for a in argv)


class FeedbackArgv:
    """Builds `agents suggest feedback accept|reject` for one idea title."""

    @staticmethod
    def build(exe: str, verb: str, project: str, title: str, reason: str, redactor) -> list:
        # The reason is redacted and put on one line; an empty reason is left out.
        argv = [exe, "--json", "agents", "suggest", "feedback", verb, "--project", project, "--suggestion", title]
        if reason and reason.strip():
            argv += ["--reason", redactor(" ".join(reason.split()))]
        return argv


class PaidRunGuard:
    """Refuses a second paid run in one output folder: the first run's model calls were already billed."""

    def __init__(self, out_dir: Path) -> None:
        # Watches paid-run.txt and result.json in out_dir.
        self.out_dir = Path(out_dir)
        self.marker = self.out_dir / "paid-run.txt"

    def problem(self):
        # Returns why a paid run must not start here, or None when it may.
        if self.marker.exists() or (self.out_dir / "result.json").exists():
            return (
                "A paid run was already started in this folder (see paid-run.txt and result.json), and the model calls it finished were billed. "
                "Read result.json if it is there. Any other run is a new usage-billed run: ask the user first, then gather into a new folder."
            )
        return None

    def mark(self) -> None:
        # Writes paid-run.txt before the paid command starts.
        TextTools.write_lf(self.marker, "Paid run started %s with --max-total-tokens %d.\n" % (dt.datetime.now().isoformat(timespec="seconds"), MAX_TOTAL_TOKENS))


class CliInvoker:
    """Finds vidbyte-cli (or the --cli override) and runs it inside the output folder."""

    def __init__(self, cli: str = None) -> None:
        # cli is a command name or a path; None means vidbyte-cli on PATH.
        self.cli = cli

    def resolve(self):
        # Returns the executable path, or None when it cannot be found.
        if self.cli:
            found = shutil.which(self.cli)
            if found:
                return found
            path = Path(self.cli).expanduser()
            return str(path) if path.is_file() else None
        return shutil.which("vidbyte-cli")

    def run(self, argv: list, out_dir: Path, stdout_name: str):
        # Runs argv in out_dir; stdout goes to stdout_name, stderr is shown live and saved. Returns (code, stdout, stderr, error).
        out_dir = Path(out_dir)
        stdout_path = out_dir / stdout_name
        stderr_path = out_dir / (Path(stdout_name).stem + ".stderr.txt")
        captured = []
        with open(str(stdout_path), "wb") as sink:
            process = subprocess.Popen(argv, cwd=str(out_dir), stdout=sink, stderr=subprocess.PIPE)
            for line in iter(process.stderr.readline, b""):
                captured.append(line)
                sys.stderr.buffer.write(line)
                sys.stderr.flush()
            process.stderr.close()
            code = process.wait()
        with open(str(stderr_path), "wb") as handle:
            handle.write(b"".join(captured))
        if code != 0 and stdout_path.exists() and stdout_path.stat().st_size == 0:
            stdout_path.unlink()
        return code, stdout_path, stderr_path, self._error(captured)

    def display(self, argv: list) -> str:
        # The command as the user would type it, with the executable shown as vidbyte-cli.
        return " ".join(shlex.quote(a) for a in ["vidbyte-cli"] + list(argv[1:]))

    @staticmethod
    def _error(captured: list):
        # The data of the last {"kind": "error"} JSON line the CLI printed on stderr, if any.
        for raw in reversed(captured):
            try:
                document = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(document, dict) and document.get("kind") == "error":
                return document.get("data")
        return None


class CostEstimator:
    """Estimates what a finished run cost from its reported usage."""

    @staticmethod
    def estimate(usage: dict, models: list):
        # Dollars at gpt-6-sol rates plus 2%, or None when no call was made or another model ran.
        if not usage or not usage.get("model_calls") or any(model != MODEL for model in models):
            return None
        cached = int(usage.get("cached_input_tokens") or 0)
        fresh = max(int(usage.get("input_tokens") or 0) - cached, 0)
        output = int(usage.get("output_tokens") or 0)
        dollars = (fresh * RATES_PER_MILLION["input"] + cached * RATES_PER_MILLION["cached_input"] + output * RATES_PER_MILLION["output"]) / 1_000_000
        return round(dollars * MARKUP, 2)


class RunReporter:
    """Turns a finished CLI call into the short JSON report the skill prints."""

    @staticmethod
    def summarize(code: int, stdout_path: Path, stderr_path: Path, error) -> dict:
        # Reports status, idea count, models, usage, estimated cost, context manifest, and warnings, or the error.
        report: dict = {"exit_code": code, "stderr_file": str(stderr_path)}
        if code == 0:
            try:
                with open(str(stdout_path), encoding="utf-8") as handle:
                    document = json.load(handle)
                data = document.get("data", {}) if isinstance(document, dict) else {}
            except (OSError, ValueError):
                document, data = {}, {}
            settings = data.get("settings") or {}
            models = list(OrderedDict.fromkeys(m for m in (settings.get("model"), settings.get("critic_model")) if m))
            usage = data.get("usage") or {}
            report.update({
                "result_file": str(stdout_path),
                "kind": document.get("kind") if isinstance(document, dict) else None,
                "status": data.get("status"),
                "stop_reason": data.get("stop_reason"),
                "ideas": len(data.get("ideas") or []),
                "models": models,
                "usage": {key: int(usage.get(key) or 0) for key in USAGE_KEYS},
                "estimated_cost_usd": CostEstimator.estimate(usage, models),
                "context_manifest": [{"ref": m.get("ref"), "kind": m.get("kind"), "status": m.get("status")} for m in data.get("context_manifest") or [] if isinstance(m, dict)],
                "warnings": data.get("warnings") or [],
            })
        elif isinstance(error, dict):
            report["error"] = {k: error.get(k) for k in ("code", "message", "hint", "retryable") if k in error}
        return report
