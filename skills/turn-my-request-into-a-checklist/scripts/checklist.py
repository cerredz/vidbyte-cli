#!/usr/bin/env python3
"""Turn the request the user just typed into a checklist with the Vidbyte Suggestion Agent.

Four subcommands, run in this order by the turn-my-request-into-a-checklist skill:

  start      Free. Makes a new output folder under the system temp folder and prints the path
             of typed.txt, where the coding agent saves the user's request exactly as typed.
  run        --dry-run is free: it reads typed.txt, redacts secrets, emails, and home paths,
             saves the exact request in request.json, and checks it with
             `vidbyte-cli agents suggest run --dry-run`. --paid replays that same request once.
             It is the one usage-billed step and needs the user's yes first.
  checklist  Free. Prints result.json as a Markdown checklist grouped by Now, Next, and Later.
  feedback   Free and local. Records the user's explicit accept or reject of one item.

Only the Python standard library is used, and Python 3.8 or newer is enough. vidbyte-cli is
started as an argument list without a shell, so the user's text is never parsed by bash, zsh,
PowerShell, or cmd.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path

PROJECT_KEY = "request-checklists"
# What a checklist needs: what must be true first, what to do now, how to check it, what to guard.
CATEGORIES = ("prerequisite", "immediate_next_steps", "verification", "risk_prevention")
HORIZON = "any"  # Any other value drops ideas after review, and the run is still billed.
DEFAULT_COUNT = 8  # Eight items fit on one screen; a larger count asks for longer replies.
# The run starts no new model call once it has used this many tokens, which bounds its cost.
MAX_TOTAL_TOKENS = 400000

# vidbyte-cli's default model for both roles, and its regular rates in US dollars per million
# tokens (https://developers.openai.com/api/docs/models/gpt-6-sol), which Vidbyte's model gateway
# bills plus a 2% markup. They only estimate a finished run's cost; vidbyte.pro/usage is exact.
MODEL = "gpt-6-sol"
RATES_PER_MILLION = {"input": 2.0, "cached_input": 0.2, "output": 10.0}
MARKUP = 1.02
USAGE_KEYS = ("model_calls", "input_tokens", "cached_input_tokens", "output_tokens")
MAX_GOAL_CHARS = 4096  # vidbyte-cli refuses a longer --goal.
MAX_SUMMARY_CHARS = 600
MAX_TEXT_CHARS = 200000
# Optional values, in the order they are passed, with the flag and the most this skill passes.
VALUE_FLAGS = OrderedDict(
    [
        ("context", ("--context", 3)),
        ("constraint", ("--constraint", 3)),
        ("completed", ("--completed", 3)),
        ("in_progress", ("--in-progress", 3)),
        ("avoid", ("--avoid", 3)),
    ]
)
# vidbyte-cli drops every idea that contains one of these values word for word.
DROPPING_KINDS = ("completed", "in_progress", "avoid")
MIN_DROPPING_WORDS = 4
MAX_VALUE_CHARS = 400
MAX_ARGV_CHARS = 24000  # Windows refuses command lines over 32,767 characters.
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
IDEA_RE = re.compile(r"^idea-\d{3}$")
TEXT_FILE = "typed.txt"
LONG_FILE = "request.md"
READINESS = {"needs_evidence": "needs evidence first", "needs_decision": "needs a decision first", "blocked": "blocked"}


class UsageError(Exception):
    """A problem the caller can fix; reported as JSON on stdout and exit code 2."""


# --------------------------------------------------------------------------------------------
# Redaction (the same patterns as pick-up-where-my-sessions-left-off/scripts/sessions.py)


class Redactor:
    """Removes secrets, emails, and home paths from any text before it is written or sent."""

    TOKEN_RULES = [
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED private key]"),
        ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
        ("token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
        ("token", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
        ("token", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[REDACTED]"),
        ("token", re.compile(r"\bvb_(?:live|test)_[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
        ("token", re.compile(r"\b0x[a-fA-F0-9]{64}\b"), "[REDACTED]"),
        ("token", re.compile(r"(?i)\b(bearer|basic)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
        ("token", re.compile(r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key)([\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED|bearer|basic)[^\s\"',;]{6,}"), r"\1\2[REDACTED]"),
        ("token", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@\"']+:[^/\s@\"']+@"), r"\1[REDACTED]@"),
        # .env-style lines: a whole line that is NAME=value, optionally quoted or exported.
        ("env_value", re.compile(r"(?m)^(\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}\s*=\s*)(?!\[REDACTED)(?:\"[^\"\n]*\"|'[^'\n]*'|\S+)[ \t]*$"), r"\1[REDACTED]"),
    ]
    # Any assignment whose name says it holds a secret: FOO_TOKEN=..., "api_key": "...", ?token=...
    SECRET_ASSIGN = re.compile(
        r"(?i)(?P<q1>[\"']?)\b(?P<name>[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|pwd|credential|private[_-]?key|access[_-]?key|auth)[A-Za-z0-9_.-]*)(?P=q1)"
        r"(?P<sep>\s*[:=]\s*)(?P<q2>[\"']?)(?!\[REDACTED)(?P<value>[^\s\"',;{}\[\]]{4,})"
    )
    # Login names that are also ordinary words; replacing them everywhere would garble the text.
    GENERIC_LOGINS = {"user", "users", "admin", "root", "test", "dev", "home", "guest", "ubuntu", "runner", "vscode", "codespace", "pi", "build", "app", "node", "owner"}
    EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
    HOME_RULES = [
        re.compile(r"(?i)\b[A-Z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+[^\\/\s\"'<>|:*?`]+"),
        re.compile(r"(?i)(?<![A-Za-z0-9])/[a-z]/Users/[^/\s\"'`]+"),
        re.compile(r"(?<![A-Za-z0-9~])/Users/[^/\s\"'`]+"),
        re.compile(r"(?<![A-Za-z0-9~])/home/[^/\s\"'`]+"),
    ]

    def __init__(self, home: Path) -> None:
        self.counts: Counter = Counter()
        spellings = set()
        raw = str(home)
        for value in (raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\"), raw.replace("/", "\\")):
            if len(value) > 3:
                spellings.add(value)
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
        if match:  # Git Bash and MSYS spell C:\Users\me as /c/Users/me.
            spellings.add("/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/"))
        if len(raw) > 3:  # Claude Code names project folders after the cwd: C--Users-me-repo.
            spellings.add(re.sub(r"[^A-Za-z0-9]", "-", raw))
        ordered = sorted(spellings, key=len, reverse=True)
        flags = re.I if os.name == "nt" else 0
        self.home_re = re.compile("|".join(re.escape(s) for s in ordered), flags) if ordered else None
        login = home.name
        self.login_re = None
        if len(login) >= 3 and login.lower() not in self.GENERIC_LOGINS:
            self.login_re = re.compile(r"(?<![A-Za-z0-9])" + re.escape(login) + r"(?![A-Za-z0-9])", re.I)

    def __call__(self, text: object) -> str:
        if text is None:
            return ""
        out = str(text)
        for kind, pattern, replacement in self.TOKEN_RULES:
            out, hits = pattern.subn(replacement, out)
            if hits:
                self.counts[kind] += hits
        out = self.SECRET_ASSIGN.sub(self._secret, out)
        out = self.EMAIL_RE.sub(self._email, out)
        if self.home_re is not None:
            out, hits = self.home_re.subn("~", out)
            self.counts["home_path"] += hits
        for pattern in self.HOME_RULES:
            out, hits = pattern.subn("~", out)
            self.counts["home_path"] += hits
        if self.login_re is not None:
            out, hits = self.login_re.subn("[user]", out)
            self.counts["login_name"] += hits
        return out

    def _secret(self, match: re.Match) -> str:
        name, value = match.group("name"), match.group("value")
        plain = not match.group("q1") and not match.group("q2") and not re.search(r"[_-]", name) and not name.isupper()
        # Prose such as "auth: flow" keeps its word; "password: hunter2" or a long value does not.
        if ":" in match.group("sep") and plain and not (re.search(r"\d", value) or len(value) >= 12):
            return match.group(0)
        self.counts["env_value"] += 1
        return "%s%s%s%s%s[REDACTED]" % (match.group("q1"), name, match.group("q1"), match.group("sep"), match.group("q2"))

    def _email(self, match: re.Match) -> str:
        if match.group(1).lower() == "git":  # git@github.com:owner/repo is an address, not a person.
            return match.group(0)
        self.counts["email"] += 1
        return "[email]"


# --------------------------------------------------------------------------------------------
# Small helpers


def one_line(text: object, limit: int = 0) -> str:
    flat = " ".join(str(text).split())
    if limit and len(flat) > limit:
        return flat[: max(limit - 3, 1)].rstrip() + "..."
    return flat


def write_text(path: Path, text: str) -> None:
    # Path.write_text has no newline= before Python 3.10.
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def load_json(path: Path, label: str):
    try:
        with open(str(path), encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise UsageError("%s not found: %s. Pass the out_dir that `start` printed as --out." % (label, path))
    except ValueError as error:
        raise UsageError("%s is not valid JSON (%s): %s" % (label, error, path))


def read_typed_text(source: str) -> str:
    """Reads the user's request from a file, or from stdin when source is '-'."""
    if source == "-":
        raw = sys.stdin.buffer.read()
    else:
        try:
            with open(source, "rb") as handle:
                raw = handle.read(MAX_TEXT_CHARS * 4 + 1)
        except FileNotFoundError:
            raise UsageError("%s not found. Save the user's request there first, exactly as typed." % source)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):  # Windows PowerShell 5.1 writes UTF-16 by default.
        text = raw.decode("utf-16", "replace")
    else:
        text = raw.decode("utf-8-sig", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT_CHARS:
        raise UsageError("The request has %d characters; this skill takes at most %d." % (len(text), MAX_TEXT_CHARS))
    return text


def display_command(argv: list) -> str:
    shown = ["vidbyte-cli"] + list(argv[1:])
    if os.name == "nt":
        return subprocess.list2cmdline(shown)
    return " ".join(shlex.quote(a) for a in shown)


def resolve_cli(value: str) -> str:
    """Returns the full path of the vidbyte-cli to call: a name looked up on PATH, or a path."""
    value = (value or "vidbyte-cli").strip()
    if os.path.dirname(value):
        candidates = [value]
        if os.name == "nt" and not os.path.splitext(value)[1]:
            candidates += [value + ext for ext in (os.environ.get("PATHEXT") or ".EXE").split(os.pathsep) if ext]
        exe = next((os.path.abspath(c) for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)), None)
        if not exe:
            raise CliMissing("--cli %s is not an executable file." % value)
    else:
        exe = shutil.which(value)
        if not exe:
            raise CliMissing("%s is not on PATH. Follow references/setup.md step 1, or pass --cli with the full path of vidbyte-cli." % value)
    if os.name == "nt" and exe.lower().endswith((".bat", ".cmd")):
        # cmd.exe would reinterpret &, |, and % in the user's text. pip and pipx install an .exe.
        raise UsageError("vidbyte-cli resolves to a batch file (%s). Reinstall it with pipx or pip so it is an .exe." % os.path.basename(exe))
    print("Using vidbyte-cli at: " + exe, file=sys.stderr)
    return exe


class CliMissing(Exception):
    """vidbyte-cli could not be found; reported with exit code 127."""


def run_cli(argv: list, out_dir: Path, stdout_name: str):
    """Runs vidbyte-cli in out_dir; saves stdout to a file, and shows and saves stderr."""
    stdout_path = out_dir / stdout_name
    stderr_path = out_dir / (Path(stdout_name).stem + ".stderr.txt")
    captured = []
    with open(str(stdout_path), "wb") as sink:
        process = subprocess.Popen(argv, cwd=str(out_dir), stdout=sink, stderr=subprocess.PIPE)
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            captured.append(line)
            sys.stderr.buffer.write(line)
            sys.stderr.flush()
        code = process.wait()
    with open(str(stderr_path), "wb") as handle:
        handle.write(b"".join(captured))
    error = None
    for raw in reversed(captured):
        try:
            document = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("kind") == "error":
            error = document.get("data")
            break
    if code != 0 and stdout_path.exists() and stdout_path.stat().st_size == 0:
        stdout_path.unlink()
    return code, stdout_path, stderr_path, error


def read_result(path: Path):
    try:
        with open(str(path), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}, {}
    data = document.get("data") if isinstance(document, dict) else None
    return document, data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------------------------
# run: build, check, and replay one request


def build_request(args, text: str, out_dir: Path, redact: Redactor) -> OrderedDict:
    problems = []
    body = redact(text)
    if not body:
        raise UsageError("The request is empty. Save the user's request, exactly as typed, to %s." % TEXT_FILE)
    summary = redact(one_line(args.goal_summary or ""))
    files = []
    if len(body) <= MAX_GOAL_CHARS:
        if summary:
            problems.append("--goal-summary is only for requests over %d characters; this one has %d. Leave it out." % (MAX_GOAL_CHARS, len(body)))
        goal = body
        title_source = body
    else:
        if not summary:
            problems.append(
                "The request has %d characters, over the %d that --goal accepts. Write a one-sentence summary of it and pass it as "
                "--goal-summary; the full redacted text is then sent as %s." % (len(body), MAX_GOAL_CHARS, LONG_FILE)
            )
        elif len(summary) > MAX_SUMMARY_CHARS:
            problems.append("--goal-summary has %d characters; keep it under %d." % (len(summary), MAX_SUMMARY_CHARS))
        goal = summary
        title_source = summary
        files = [LONG_FILE]
    values = OrderedDict()
    for kind, (flag, most) in VALUE_FLAGS.items():
        cleaned = [redact(one_line(v)) for v in (getattr(args, kind) or []) if v.strip()]
        if len(cleaned) > most:
            problems.append("%s was given %d times; this skill passes at most %d." % (flag, len(cleaned), most))
        for value in cleaned:
            if len(value) > MAX_VALUE_CHARS:
                problems.append("A %s value has %d characters; keep each under %d: %s" % (flag, len(value), MAX_VALUE_CHARS, one_line(value, 80)))
            if kind in DROPPING_KINDS and len(value.split()) < MIN_DROPPING_WORDS:
                problems.append(
                    "%s %r is too short. vidbyte-cli drops every idea that contains a %s value word for word, so write a full "
                    "sentence of at least %d words." % (flag, value, flag, MIN_DROPPING_WORDS)
                )
        values[kind] = cleaned
    count = args.count
    if not 2 <= count <= 100:
        problems.append("--count must be between 2 and 100.")
    project = None if args.no_project else (args.project or PROJECT_KEY)
    if project is not None and not PROJECT_RE.match(project):
        problems.append("--project must be 1-64 lowercase letters, digits, hyphens, or underscores.")
    if problems:
        raise UsageError(" ".join(problems))
    long_path = out_dir / LONG_FILE
    if files:
        write_text(long_path, "# The user's request (redacted)\n\n" + body + "\n")
    elif long_path.exists():
        long_path.unlink()
    first_line = next((line for line in title_source.split("\n") if line.strip()), title_source)
    return OrderedDict(
        [
            ("title", one_line(first_line, 90)),
            ("goal", goal),
            ("categories", list(CATEGORIES)),
            ("horizon", HORIZON),
            ("count", count),
            ("values", values),
            ("files", files),
            ("project", project),
        ]
    )


def build_argv(exe: str, request: dict, dry_run: bool) -> list:
    argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", request["goal"]]
    for category in request["categories"]:
        argv += ["--category", category]
    argv += ["--horizon", request["horizon"], "--count", str(request["count"])]
    for kind, (flag, _most) in VALUE_FLAGS.items():
        for value in request["values"].get(kind, []):
            argv += [flag, value]
    for name in request["files"]:
        argv += ["--files", name]  # Relative: vidbyte-cli runs inside out_dir, and the path is recorded.
    if request["project"]:
        argv += ["--project", request["project"]]
    argv += ["--max-total-tokens", str(MAX_TOTAL_TOKENS)]  # On both runs, so the dry run checks the paid command.
    if dry_run:
        argv.append("--dry-run")
    return argv


def run_models(data: dict) -> list:
    """The generator's and the critic's model, from a result's settings, without repeats."""
    settings = data.get("settings") or {}
    return list(OrderedDict.fromkeys(m for m in (settings.get("model"), settings.get("critic_model")) if m))


def estimate_usd(usage: dict, models: list):
    """Estimates a finished run's cost in dollars, or None when it made no call or left gpt-6-sol."""
    if not usage.get("model_calls") or any(model != MODEL for model in models):
        return None
    cached = int(usage.get("cached_input_tokens") or 0)
    fresh = max(int(usage.get("input_tokens") or 0) - cached, 0)
    output = int(usage.get("output_tokens") or 0)
    rates = RATES_PER_MILLION
    dollars = (fresh * rates["input"] + cached * rates["cached_input"] + output * rates["output"]) / 1000000
    return round(dollars * MARKUP, 2)


def usage_line(data: dict) -> str:
    """One sentence on what a finished run used, or an empty string when it made no model call."""
    usage = data.get("usage") or {}
    if not usage.get("model_calls"):
        return ""
    estimate = estimate_usd(usage, run_models(data))
    return "This run made %d model calls with %d input and %d output tokens, %s from your Vidbyte balance; the exact amount is on vidbyte.pro/usage." % (
        int(usage["model_calls"]), int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
        "billed" if estimate is None else "about $%.2f" % estimate)


def fingerprint(request: dict, out_dir: Path) -> str:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for name in request.get("files") or []:
        with open(str(out_dir / name), "rb") as handle:
            digest.update(hashlib.sha256(handle.read()).hexdigest().encode("ascii"))
    return digest.hexdigest()


def cmd_run(args) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    if not out_dir.is_dir():
        raise UsageError("--out %s is not a folder. Run `start` first and pass the out_dir it printed." % out_dir)
    if args.dry_run == args.paid:
        raise UsageError("Pass exactly one of --dry-run (the free check) or --paid (the usage-billed run, only after the user said yes).")
    given = [flag for flag, value in (
        ("--text-file", args.text_file), ("--goal-summary", args.goal_summary), ("--context", args.context),
        ("--constraint", args.constraint), ("--completed", args.completed), ("--in-progress", args.in_progress),
        ("--avoid", args.avoid), ("--count", args.count != DEFAULT_COUNT), ("--project", args.project), ("--no-project", args.no_project),
    ) if value]
    if args.paid and given:
        raise UsageError("--paid replays the request the dry run checked, so it takes no %s. Pass them to --dry-run, show the user, then run --paid." % ", ".join(given))
    exe = resolve_cli(args.cli)
    request_path = out_dir / "request.json"
    ok_path = out_dir / "dry-run.ok"
    marker = out_dir / "paid-run.txt"
    result_path = out_dir / "result.json"
    redact = Redactor(Path.home())
    if args.dry_run:
        if marker.exists() or result_path.exists():
            raise UsageError("A paid run was already started in this folder. A new request needs a new folder: run `start` again.")
        text = read_typed_text(args.text_file or str(out_dir / TEXT_FILE))
        if ok_path.exists():
            ok_path.unlink()
        request = build_request(args, text, out_dir, redact)
        write_text(request_path, json.dumps(request, indent=2) + "\n")
        argv = build_argv(exe, request, True)
        if "--dry-run" not in argv:  # Belt and braces: never call a model here.
            raise UsageError("Internal check failed: the dry-run command is not a dry run.")
    else:
        if not request_path.is_file():
            raise UsageError("No passing dry run in this folder. Run `run --dry-run`, show the user what will be sent, and ask before --paid.")
        request = load_json(request_path, "request.json")
        try:
            with open(str(ok_path), encoding="utf-8") as handle:
                passed = handle.read().strip() == fingerprint(request, out_dir)
        except (OSError, ValueError, TypeError, AttributeError):
            passed = False
        if not passed:
            raise UsageError("No passing dry run matches request.json in this folder. Run `run --dry-run`, show the user what will be sent, and ask before --paid.")
        if marker.exists() or result_path.exists():
            # The model calls an earlier paid run here finished were billed, and nothing recovers
            # them. Another run is a new charge, so it needs the user's yes and a new folder.
            raise UsageError(
                "A paid run was already started in this folder (see paid-run.txt and result.json), and the model calls it finished were billed. "
                "Read result.json if it is there. Any other run is a new usage-billed run: ask the user first, then run `start` again."
            )
        argv = build_argv(exe, request, False)
        if "--dry-run" in argv:
            raise UsageError("Internal check failed: the paid command contains --dry-run.")
    length = sum(len(a) + 3 for a in argv)
    if length > MAX_ARGV_CHARS:
        raise UsageError("The command would be %d characters; shorten the --context and other values to stay under %d." % (length, MAX_ARGV_CHARS))
    if args.paid:
        write_text(marker, "Paid run started %s with --max-total-tokens %d.\n" % (dt.datetime.now().isoformat(timespec="seconds"), MAX_TOTAL_TOKENS))
    print("Running in %s:\n%s" % (out_dir, display_command(argv)), file=sys.stderr)
    code, stdout_path, stderr_path, error = run_cli(argv, out_dir, "dry-run.json" if args.dry_run else "result.json")
    report = OrderedDict([("mode", "dry-run" if args.dry_run else "paid"), ("exit_code", code), ("out_dir", str(out_dir)), ("cli", exe)])
    if args.dry_run:
        sent = OrderedDict([("goal", request["goal"])])
        if request["files"]:
            sent["files"] = request["files"]
            sent["request_md"] = str(out_dir / LONG_FILE)
        for kind, values in request["values"].items():
            if values:
                sent[kind] = values
        report.update(
            [
                ("sent", sent),
                ("redactions", OrderedDict((k, v) for k, v in sorted(redact.counts.items()) if v)),
                ("count", request["count"]),
                ("max_total_tokens", MAX_TOTAL_TOKENS),
                ("project", request["project"]),
            ]
        )
    report["command"] = display_command(argv)
    report["stderr_file"] = str(stderr_path)
    if code == 0:
        document, data = read_result(stdout_path)
        manifest = data.get("context_manifest") or []
        models = run_models(data)
        usage = data.get("usage") or {}
        report.update(
            [
                ("result_file", str(stdout_path)),
                ("kind", document.get("kind")),
                ("status", data.get("status")),
                ("stop_reason", data.get("stop_reason")),
                ("ideas", len(data.get("ideas") or [])),
                ("models", models),
                ("usage", OrderedDict((key, int(usage.get(key) or 0)) for key in USAGE_KEYS)),
                ("estimated_cost_usd", estimate_usd(usage, models)),
                ("context_manifest", [OrderedDict((k, m.get(k)) for k in ("ref", "kind", "source", "chars", "status")) for m in manifest]),
                ("warnings", data.get("warnings") or []),
            ]
        )
        if args.dry_run:
            ready = data.get("stop_reason") == "dry_run" and all(m.get("status") == "included" for m in manifest)
            if ready:
                write_text(ok_path, fingerprint(request, out_dir) + "\n")
            report["ready_for_paid"] = ready
    elif error:
        report["error"] = OrderedDict((k, error.get(k)) for k in ("code", "message", "hint", "retryable") if k in error)
    print(json.dumps(report, indent=2))
    return code


# --------------------------------------------------------------------------------------------
# checklist: result.json as Markdown


def render_checklist(data: dict, title: str, result_path: Path) -> str:
    ideas = sorted((i for i in data.get("ideas") or [] if isinstance(i, dict)), key=lambda i: i.get("rank") or 0)
    lines = ["# Checklist: " + title, ""]
    if data.get("status") == "needs_input" and data.get("parent_message"):
        lines += [
            "The Suggestion Agent stopped to ask a question before it could finish:",
            "",
            "> " + one_line(data["parent_message"]),
            "",
            "A run with the answer added is a new usage-billed run.",
            "",
        ]
    elif not ideas:
        lines += ["No checklist items came back (stop reason: %s)." % data.get("stop_reason"), ""]
    groups = OrderedDict((h, []) for h in ("now", "next", "later", "other"))
    for idea in ideas:
        groups[idea.get("horizon") if idea.get("horizon") in groups else "other"].append(idea)
    for horizon, members in groups.items():
        if not members:
            continue
        lines += ["## " + horizon.capitalize(), ""]
        for idea in members:
            notes = [str(idea.get("id"))]
            if idea.get("readiness") in READINESS:
                notes.append(READINESS[idea["readiness"]])
            lines.append("- [ ] " + one_line(idea.get("title") or ""))
            lines.append("  - First step: %s (%s)" % (one_line(idea.get("first_action") or "", 280), ", ".join(notes)))
        lines.append("")
    for warning in data.get("warnings") or []:
        lines += ["Note: " + one_line(warning), ""]
    lines += ["---", ""]
    footer = "%d items from the Vidbyte Suggestion Agent." % len(ideas)
    if usage_line(data):
        footer += " " + usage_line(data)
    lines.append(footer)
    if ideas:
        lines += [
            "",
            "- Start an item: `vidbyte-cli --json agents suggest handoff --input \"%s\" --idea <id>`" % result_path,
            "- Teach later checklists: tell your agent which items you accept or reject, and why.",
        ]
    return "\n".join(lines) + "\n"


def cmd_checklist(args) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    result_path = Path(args.result).expanduser().resolve() if args.result else out_dir / "result.json"
    document = load_json(result_path, "result.json")
    if not isinstance(document, dict) or document.get("kind") != "suggestions.result" or not isinstance(document.get("data"), dict):
        raise UsageError("%s is not a suggestions.result document." % result_path)
    data = document["data"]
    if data.get("stop_reason") == "dry_run":
        raise UsageError("%s is a dry run; it has no items. Run `run --paid` after the user's yes." % result_path)
    request_path = out_dir / "request.json"
    request = load_json(request_path, "request.json") if request_path.exists() else {}
    title = (request.get("title") if isinstance(request, dict) else None) or one_line(data.get("goal") or "your request", 90)
    text = render_checklist(data, title, result_path)
    if args.save:
        target = Path(args.save).expanduser()
        if target.exists():
            raise UsageError("%s already exists. Ask the user for another file name; this script never overwrites a file." % target)
        if not target.parent.is_dir():
            raise UsageError("The folder %s does not exist." % target.parent)
        write_text(target, text)
        print("Saved the checklist to %s" % target.resolve(), file=sys.stderr)
    write_text(out_dir / "checklist.md", text)
    sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------------------------
# feedback


def cmd_feedback(args) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    if not IDEA_RE.match(args.idea):
        raise UsageError("--idea must be an id such as idea-002.")
    _document, data = read_result(out_dir / "result.json")
    match = [i for i in data.get("ideas") or [] if isinstance(i, dict) and i.get("id") == args.idea]
    if not match:
        raise UsageError("No item with id %s in %s." % (args.idea, out_dir / "result.json"))
    project = args.project
    if not project:
        request_path = out_dir / "request.json"
        request = load_json(request_path, "request.json") if request_path.exists() else {}
        project = request.get("project") if isinstance(request, dict) else None
    if not project or not PROJECT_RE.match(project):
        raise UsageError("No suggestion project. Create %s first (see the skill's Gather inputs), then pass --project %s." % (PROJECT_KEY, PROJECT_KEY))
    exe = resolve_cli(args.cli)
    verb = "accept" if args.accept else "reject"
    # Rejection removes later ideas that repeat this text word for word, so copy it exactly.
    argv = [exe, "--json", "agents", "suggest", "feedback", verb, "--project", project, "--suggestion", match[0]["title"]]
    if args.reason and args.reason.strip():
        argv += ["--reason", Redactor(Path.home())(one_line(args.reason))]
    print(display_command(argv), file=sys.stderr)
    return subprocess.call(argv)


# --------------------------------------------------------------------------------------------
# start


def cmd_start(_args) -> int:
    out_dir = Path(tempfile.mkdtemp(prefix="request-checklist-")).resolve()
    report = OrderedDict(
        [
            ("out_dir", str(out_dir)),
            ("text_file", str(out_dir / TEXT_FILE)),
            ("next", "Save the user's request, exactly as typed, to text_file. Then run: run --out <out_dir> --dry-run"),
        ]
    )
    print(json.dumps(report, indent=2))
    return 0


# --------------------------------------------------------------------------------------------
# Command line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="checklist.py",
        description="Turn the request the user just typed into a checklist with the Vidbyte Suggestion Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 checklist.py start\n"
            "  python3 checklist.py run --out <out_dir> --dry-run --context \"Repository: shop-api, branch main.\"\n"
            "  python3 checklist.py run --out <out_dir> --paid        # billed by usage; ask the user first\n"
            "  python3 checklist.py checklist --out <out_dir>\n"
            "  python3 checklist.py feedback --out <out_dir> --idea idea-002 --reject --reason \"we already have this\"\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start", help="Make a new output folder and print where to save the request (free).")

    run = sub.add_parser("run", help="Check the request for free (--dry-run), or run it once (--paid, billed by usage).", description="--dry-run reads typed.txt, redacts it, saves request.json, and runs `vidbyte-cli --json --no-input agents suggest run ... --dry-run` inside <out>. --paid replays the same request once, only after a passing dry run, and saves result.json. Prints a JSON report and exits with vidbyte-cli's exit code.")
    run.add_argument("--out", required=True, help="The out_dir that `start` printed.")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="The free check: no model call, no charge.")
    mode.add_argument("--paid", action="store_true", help="The usage-billed run. Use only after the user said yes to the cost.")
    run.add_argument("--text-file", help="Where the request is saved (default: typed.txt in --out). '-' reads stdin.")
    run.add_argument("--goal-summary", help="A one-sentence summary, needed only when the request is over 4,096 characters.")
    run.add_argument("--context", action="append", help="A short fact you already know, such as the repository and branch. At most 3.")
    run.add_argument("--constraint", action="append", help="A limit the user stated. At most 3.")
    run.add_argument("--completed", action="append", help="Work the user said is already done, as a full sentence. At most 3.")
    run.add_argument("--in-progress", dest="in_progress", action="append", help="Work the user said is underway, as a full sentence. At most 3.")
    run.add_argument("--avoid", action="append", help="A direction the user does not want, as a full sentence. At most 3.")
    run.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Most checklist items, 2-100 (default: 8). A larger count asks for longer replies, which are more output tokens.")
    run.add_argument("--project", help="The learning project (default: %s)." % PROJECT_KEY)
    run.add_argument("--no-project", action="store_true", help="Run without a learning project.")
    run.add_argument("--cli", default="vidbyte-cli", metavar="PATH", help="The vidbyte-cli to call: a name on PATH or a full path (default: vidbyte-cli).")

    checklist = sub.add_parser("checklist", help="Print result.json as a Markdown checklist (free).", description="Print <out>/result.json as a Markdown checklist grouped by Now, Next, and Later, and save it as <out>/checklist.md.")
    checklist.add_argument("--out", required=True, help="The out_dir of the paid run.")
    checklist.add_argument("--result", help="Another result.json to read (default: result.json in --out).")
    checklist.add_argument("--save", metavar="PATH", help="Also save the checklist to this new file, only when the user asks. Never overwrites.")

    feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one item (free, local).", description="Copy one item's exact title from <out>/result.json into `vidbyte-cli --json agents suggest feedback accept|reject`.")
    feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    feedback.add_argument("--idea", required=True, help="The item id, such as idea-002.")
    verdict = feedback.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true", help="The user said they want this item.")
    verdict.add_argument("--reject", action="store_true", help="The user said no to this item.")
    feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
    feedback.add_argument("--project", help="The learning project (default: the one in request.json).")
    feedback.add_argument("--cli", default="vidbyte-cli", metavar="PATH", help="The vidbyte-cli to call: a name on PATH or a full path (default: vidbyte-cli).")

    args = parser.parse_args(argv)
    commands = {"start": cmd_start, "run": cmd_run, "checklist": cmd_checklist, "feedback": cmd_feedback}
    if args.command not in commands:
        parser.print_help()
        return 2
    try:
        return commands[args.command](args)
    except UsageError as error:
        print(json.dumps({"error": str(error)}, indent=2))
        print("Error: " + str(error), file=sys.stderr)
        return 2
    except CliMissing as error:
        print(json.dumps({"error": str(error)}, indent=2))
        print("Error: " + str(error), file=sys.stderr)
        return 127


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:  # A redirected Windows console may not be UTF-8; never fail on one character.
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
