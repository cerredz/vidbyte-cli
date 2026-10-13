#!/usr/bin/env python3
"""Verification script for skills/turn-corrections-into-skills (design doc section 10).

Builds synthetic Claude Code and Codex stores in a temporary home, imports the skill's modules for
unit tests, and runs corrections.py in subprocesses whose HOME, harness folders, and PATH all point
into that home. Every suggest and feedback call goes to a recording fake vidbyte-cli passed with
--cli; PATH holds no vidbyte-cli, so an ignored --cli fails with 127 instead of reaching a real
(billing) CLI. Run it with every Python you support:

    python scripts/test-turn-corrections-into-skills.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter, OrderedDict
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "turn-corrections-into-skills"
SCRIPTS = SKILL / "scripts"
ENTRY = SCRIPTS / "corrections.py"
FOCUS = SKILL / "references" / "skills-from-corrections.md"
sys.path.insert(0, str(SCRIPTS))

from correctionkit.detector import CorrectionDetector, CorrectionRules  # noqa: E402
from correctionkit.digest import DigestWriter  # noqa: E402
from correctionkit.inventory import SkillInventory  # noqa: E402
from correctionkit.report import SkillIdeaParser  # noqa: E402
from correctionkit.scenarios import MistakeKinds, ScenarioBuilder  # noqa: E402
from correctionkit.suggest import CorrectionSuggestConfig  # noqa: E402
from sessionkit.model import Clock, Session, TextTools  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402

SECRET_KEY = "sk-abcdefghijklmnopqrstuvwx1234"
SECRET_EMAIL = "bob.builder@example.com"
TAG_RE = re.compile(r"\[([CPS]\d+(?:[ ,]+[CPS]\d+)*)\]")
STRIP_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "VIDBYTE_API_KEY", "PYTHONPATH")
FAKE_CLI_SOURCE = r'''
import json, os, sys

def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv[:5] == ["--json", "--no-input", "agents", "suggest", "run"]:
        if "--dry-run" in argv:
            data = {"status": "no_suggestions", "stop_reason": "dry_run", "ideas": [], "settings": {"model": "gpt-6-sol", "critic_model": "gpt-6-sol"},
                    "usage": {"model_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
                    "context_manifest": [{"ref": "ctx-001", "kind": "file", "status": "included"}], "warnings": []}
            print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": data}))
            return 0
        if os.environ.get("FAKE_CLI_MODE") == "credits":
            sys.stderr.write("working...\n")
            sys.stderr.write(json.dumps({"kind": "error", "data": {"code": "insufficient_credits", "message": "Out of credits.", "hint": "Top up first.", "retryable": False}}) + "\n")
            return 5
        with open(os.environ["FAKE_CLI_RESULT"], encoding="utf-8") as handle:
            sys.stdout.write(handle.read())
        return 0
    print(json.dumps({"schema_version": 1, "kind": "suggestions.feedback", "data": {"recorded": True}}))
    return 0
'''


class Results:
    """Collects PASS/FAIL per test and prints the summary."""

    def __init__(self) -> None:
        # Starts with no results.
        self.passed = 0
        self.failed: list = []

    def run(self, name: str, category: str, test) -> None:
        # Runs one test callable; an AssertionError or any exception is a FAIL with its reason.
        try:
            test()
        except Exception as error:  # Every failure, including crashes, is reported, not raised.
            self.failed.append(name)
            print("FAIL [%s] %s -- %s" % (category, name, str(error) or type(error).__name__))
            if not isinstance(error, AssertionError):
                traceback.print_exc()
            return
        self.passed += 1
        print("PASS [%s] %s" % (category, name))

    def finish(self) -> int:
        # Prints "X/Y tests passed" and returns the process exit code.
        total = self.passed + len(self.failed)
        print("\n%d/%d tests passed (Python %s)" % (self.passed, total, sys.version.split()[0]))
        return 0 if not self.failed else 1


class Check:
    """Small assertion helpers with readable messages."""

    @staticmethod
    def equal(actual, expected, label: str = "") -> None:
        # Fails with both values when they differ.
        assert actual == expected, "%s expected %r, got %r" % (label, expected, actual)

    @staticmethod
    def true(value, label: str) -> None:
        # Fails with the label when value is falsy.
        assert value, label


class Stamp:
    """Fixture timestamps relative to a base two days ago."""

    BASE = time.time() - 2 * 86400

    @staticmethod
    def at(minutes: float) -> float:
        # Epoch seconds `minutes` after the base.
        return Stamp.BASE + minutes * 60

    @staticmethod
    def iso(minutes: float) -> str:
        # An ISO 8601 UTC string like Claude Code and Codex write.
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(Stamp.at(minutes))) + ".123Z"

    @staticmethod
    def ms(minutes: float) -> int:
        # Epoch milliseconds, as Pi writes in message timestamps.
        return int(Stamp.at(minutes) * 1000)


class Writer:
    """Writes fixture files with LF endings and sets their modification times."""

    @staticmethod
    def jsonl(path: Path, records: list, minutes: float) -> Path:
        # Writes records as JSON lines and sets the mtime.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        os.utime(str(path), (Stamp.at(minutes), Stamp.at(minutes)))
        return path

    @staticmethod
    def text(path: Path, text: str) -> Path:
        # Writes a UTF-8 text file with LF endings.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


class ClaudeFixtures:
    """Claude Code transcript records."""

    @staticmethod
    def user(content, minutes: float, cwd: str) -> dict:
        # A user record with string content (a prompt) or list content (tool results).
        return {"type": "user", "timestamp": Stamp.iso(minutes), "cwd": cwd, "sessionId": "x", "entrypoint": "cli", "message": {"role": "user", "content": content}}

    @staticmethod
    def assistant(minutes: float, cwd: str, text: str = "", tool: tuple = None) -> dict:
        # An assistant record with text, or one Bash tool call (id, command).
        parts = [{"type": "text", "text": text}] if text else []
        if tool:
            parts.append({"type": "tool_use", "id": tool[0], "name": "Bash", "input": {"command": tool[1]}})
        return {"type": "assistant", "timestamp": Stamp.iso(minutes), "cwd": cwd, "message": {"role": "assistant", "model": "claude-opus-5-5", "content": parts, "stop_reason": None if tool else "end_turn"}}

    @staticmethod
    def result(tool_id: str, minutes: float, cwd: str, error: bool) -> dict:
        # A tool result, failed or not.
        return ClaudeFixtures.user([{"type": "tool_result", "tool_use_id": tool_id, "content": "npm ERR!" if error else "ok", "is_error": error}], minutes, cwd)

    @staticmethod
    def session_a(cwd: str) -> list:
        # npm install fails; the user says "no, use pnpm not npm" (with a secret and an email); then "thanks".
        u, a, r = ClaudeFixtures.user, ClaudeFixtures.assistant, ClaudeFixtures.result
        return [
            u("add the lodash dependency to the web app", 1, cwd), a(2, cwd, tool=("t1", "npm install lodash")), r("t1", 3, cwd, True),
            a(4, cwd, "I installed lodash with npm."),
            u("no, use pnpm not npm in this repo, my key is %s and mail %s" % (SECRET_KEY, SECRET_EMAIL), 5, cwd),
            a(6, cwd, tool=("t2", "pnpm add lodash")), r("t2", 7, cwd, False), a(8, cwd, "Done with pnpm."),
            u("thanks", 9, cwd), a(10, cwd, "You're welcome."),
        ]

    @staticmethod
    def session_b(cwd: str) -> list:
        # npm install fails again in another session; the user supplies the fix "use pnpm, not npm".
        u, a, r = ClaudeFixtures.user, ClaudeFixtures.assistant, ClaudeFixtures.result
        return [
            u("add zod to the web app", 20, cwd), a(21, cwd, tool=("t3", "npm install zod")), r("t3", 22, cwd, True), a(23, cwd, "Installed zod."),
            u("use pnpm, not npm", 24, cwd), a(25, cwd, tool=("t4", "pnpm add zod")), r("t4", 26, cwd, False), a(27, cwd, "Done."),
        ]

    @staticmethod
    def calm_session(cwd: str) -> list:
        # A session with no correction at all.
        u, a = ClaudeFixtures.user, ClaudeFixtures.assistant
        return [u("add a README for the web app", 80, cwd), a(81, cwd, "Added README.md."), u("thanks, looks good", 82, cwd), a(83, cwd, "Glad it helps.")]


class CodexFixtures:
    """Codex rollout records (top-level key order matches the real files)."""

    @staticmethod
    def rec(kind: str, payload: dict, minutes: float) -> dict:
        # One rollout line: timestamp, type, payload.
        return OrderedDict([("timestamp", Stamp.iso(minutes)), ("type", kind), ("payload", payload)])

    @staticmethod
    def call(command: str, minutes: float, exit_code: int) -> list:
        # A shell function call and its output with an exit code.
        r = CodexFixtures.rec
        return [
            r("response_item", OrderedDict([("type", "function_call"), ("name", "shell"), ("arguments", json.dumps({"command": ["bash", "-lc", command]})), ("call_id", "c%s" % minutes)]), minutes),
            r("response_item", OrderedDict([("type", "function_call_output"), ("call_id", "c%s" % minutes), ("output", "Exit code: %d\n" % exit_code)]), minutes + 0.2),
        ]

    @staticmethod
    def head(session_id: str, cwd: str, minutes: float) -> list:
        # session_meta and turn_context for an interactive CLI session.
        r = CodexFixtures.rec
        return [r("session_meta", OrderedDict([("id", session_id), ("cwd", cwd), ("originator", "codex_cli_rs"), ("source", "cli")]), minutes),
                r("turn_context", OrderedDict([("cwd", cwd), ("model", "gpt-6-sol")]), minutes + 0.1)]

    @staticmethod
    def said(text: str, minutes: float) -> list:
        # A final agent message and the end of the turn.
        r = CodexFixtures.rec
        return [r("event_msg", OrderedDict([("type", "agent_message"), ("message", text), ("phase", "final_answer")]), minutes),
                r("event_msg", OrderedDict([("type", "task_complete"), ("last_agent_message", text)]), minutes + 0.1)]

    @staticmethod
    def prompt(text: str, minutes: float) -> dict:
        # A user_message event.
        return CodexFixtures.rec("event_msg", OrderedDict([("type", "user_message"), ("message", text)]), minutes)

    @staticmethod
    def session_c(cwd: str) -> list:
        # The agent claims the test passes; two corrections in a row ("still failing", "you didn't run pytest").
        c = CodexFixtures
        return (c.head("codex-c", cwd, 40) + [c.prompt("fix the failing login test", 40.5)] + c.call("pytest tests/test_login.py", 41, 1)
                + c.said("Fixed, the test passes now.", 42) + [c.prompt("it's still failing, did you even run the tests?", 43)] + c.call("pytest -q", 44, 0)
                + c.said("Passing now.", 45) + [c.prompt("you didn't run pytest on the login test again, run it", 46)] + c.call("pytest tests/test_login.py", 47, 0)
                + c.said("All green.", 48))

    @staticmethod
    def session_d(cwd: str) -> list:
        # The user interrupts a refactor and redirects it: "stop, edit api.py instead".
        c = CodexFixtures
        return (c.head("codex-d", cwd, 60) + [c.prompt("refactor the auth module", 60.5)] + c.call("sed -i s/a/b/ auth.py", 61, 0)
                + [c.rec("event_msg", OrderedDict([("type", "turn_aborted"), ("reason", "interrupted")]), 62), c.prompt("stop, edit api.py instead", 63)]
                + c.call("apply_patch api.py", 64, 0) + c.said("Edited api.py.", 65))


class MessageLogFixtures:
    """Factory Droid and Pi message logs (shapes from the sessionkit v4 reference test)."""

    @staticmethod
    def msg(minutes: float, message: dict) -> dict:
        # One {"type": "message"} record.
        return {"type": "message", "id": "m%s" % minutes, "timestamp": Stamp.iso(minutes), "message": message}

    @staticmethod
    def text(value: str) -> dict:
        # One text content part.
        return {"type": "text", "text": value}

    @staticmethod
    def factory(cwd: str) -> list:
        # A reminder-only message, a prompt with a reminder part, a UI notice, and a reply with a tool call; no correction.
        m, t = MessageLogFixtures.msg, MessageLogFixtures.text
        return [
            {"type": "session_start", "id": "fac-1", "title": "New Session", "sessionTitle": "Fix login", "owner": "me", "version": 2, "cwd": cwd},
            m(100, {"role": "user", "visibility": "llm_only", "content": [t("<system-reminder>tools</system-reminder>")]}),
            m(100.1, {"role": "user", "content": [t("<system-reminder>x</system-reminder>"), t("Fix the login redirect in app/auth.ts")]}),
            m(100.2, {"role": "user", "visibility": "user_only", "content": [t("No active subscription")]}),
            m(101, {"role": "assistant", "content": [t("Fixing."), {"type": "tool_use", "id": "t", "name": "Edit", "input": {"file_path": "app/auth.ts"}}]}),
            m(101.5, {"role": "assistant", "content": [t("Fixed: the redirect now goes to /home.")]}),
        ]

    @staticmethod
    def pi(cwd: str) -> list:
        # A failed tool call, an aborted reply, and the correction "no, list app/ instead".
        m, t = MessageLogFixtures.msg, MessageLogFixtures.text
        return [
            {"type": "session", "version": 3, "id": "pi-1", "timestamp": Stamp.iso(110), "cwd": cwd},
            {"type": "model_change", "id": "x", "parentId": None, "timestamp": Stamp.iso(110), "provider": "deepseek", "modelId": "deepseek-v4-pro"},
            m(110.1, {"role": "user", "content": [t("List the files in src/")], "timestamp": Stamp.ms(110.1)}),
            m(110.2, {"role": "assistant", "model": "deepseek-v4-pro", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls src"}}], "stopReason": "toolUse"}),
            m(110.3, {"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": True, "content": [t("no such dir")]}),
            m(110.4, {"role": "assistant", "model": "deepseek-v4-pro", "content": [], "stopReason": "aborted"}),
            m(111, {"role": "user", "content": [t("no, list app/ instead")]}),
            m(111.5, {"role": "assistant", "model": "deepseek-v4-pro", "content": [t("Here they are.")], "stopReason": "stop"}),
        ]


class Home:
    """A temporary home folder holding the fixture stores."""

    def __init__(self, path: Path, kind: str = "main") -> None:
        # Builds the fixtures of one kind: main (4 sessions with corrections), calm (no corrections), badopencode, or moreharness.
        self.path = path
        self.web = str(path / "code" / "web")
        self.api = str(path / "code" / "api")
        getattr(self, "build_" + kind)()

    def build_main(self) -> None:
        # Two Claude Code sessions in web, two Codex sessions in api, and two existing skills.
        h = self.path
        Writer.jsonl(h / ".claude" / "projects" / "C--code-web" / "claude-a.jsonl", ClaudeFixtures.session_a(self.web), 10)
        Writer.jsonl(h / ".claude" / "projects" / "C--code-web" / "claude-b.jsonl", ClaudeFixtures.session_b(self.web), 27)
        codex = h / ".codex" / "sessions" / "2026" / "10" / "11"
        Writer.jsonl(codex / "rollout-c.jsonl", CodexFixtures.session_c(self.api), 48)
        Writer.jsonl(codex / "rollout-d.jsonl", CodexFixtures.session_d(self.api), 65)
        Writer.text(h / ".claude" / "skills" / "deploy-app" / "SKILL.md", "---\nname: deploy-app\ndescription: Use when deploying the app.\n---\n# Deploy\n")
        Writer.text(Path(self.web) / ".agents" / "skills" / "web-style" / "SKILL.md", "---\nname: web-style\ndescription: >\n  Use when styling\n  web components.\n---\n")

    def build_calm(self) -> None:
        # One Claude Code session with no correction.
        Writer.jsonl(self.path / ".claude" / "projects" / "C--code-web" / "claude-calm.jsonl", ClaudeFixtures.calm_session(self.web), 83)

    def build_badopencode(self) -> None:
        # One Claude Code session with a correction, and an opencode.db that is not SQLite.
        Writer.jsonl(self.path / ".claude" / "projects" / "C--code-web" / "claude-a.jsonl", ClaudeFixtures.session_a(self.web), 10)
        Writer.text(self.path / ".local" / "share" / "opencode" / "opencode.db", "this is not a database" * 50)

    def build_moreharness(self) -> None:
        # One OpenClaude session with the pnpm correction, one Factory Droid session without one, one Pi session with one, and an OpenClaude skill.
        h = self.path
        Writer.jsonl(h / ".openclaude" / "projects" / "C--code-web" / "openclaude-b.jsonl", ClaudeFixtures.session_b(self.web), 27)
        Writer.text(h / ".openclaude" / "skills" / "web-install" / "SKILL.md", "---\nname: web-install\ndescription: Use when installing web packages.\n---\n")
        Writer.jsonl(h / ".factory" / "sessions" / "-C-code-api" / "fac-1.jsonl", MessageLogFixtures.factory(self.api), 101.5)
        Writer.text(h / ".factory" / "sessions" / "-C-code-api" / "fac-1.settings.json", json.dumps({"model": "x"}))
        Writer.jsonl(h / ".pi" / "agent" / "sessions" / "--C--code-api--" / "2026-10-01T00-00-00-000Z_pi-1.jsonl", MessageLogFixtures.pi(self.api), 111.5)


class FakeCli:
    """A recording fake vidbyte-cli: a real launcher (an .exe on Windows) made with pip's distlib."""

    def __init__(self, base: Path) -> None:
        # Writes fakecli.py and builds the launcher in base/fakebin.
        self.module_dir = base / "fakecli"
        Writer.text(self.module_dir / "fakecli.py", FAKE_CLI_SOURCE)
        self.bin_dir = base / "fakebin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.log = base / "fakecli-log.jsonl"
        from pip._vendor.distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, str(self.bin_dir))
        maker.executable = sys.executable
        maker.variants = {""}
        self.path = maker.make("vidbyte-cli = fakecli:main")[0]

    def calls(self) -> list:
        # Every recorded call: {"argv": [...], "cwd": "..."}.
        if not self.log.exists():
            return []
        with open(str(self.log), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class Runner:
    """Runs corrections.py in a subprocess confined to one fixture home."""

    def __init__(self, home: Home, fake: FakeCli, base: Path) -> None:
        # Builds the confined environment once.
        self.home = home
        self.fake = fake
        self.env = self.environment(base)

    def run(self, *args: str, extra_env: dict = None):
        # Returns (exit code, parsed stdout JSON or None, stdout text, stderr text).
        env = dict(self.env)
        env.update(extra_env or {})
        process = subprocess.run([sys.executable, str(ENTRY)] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        out = process.stdout.decode("utf-8", "replace")
        try:
            document = json.loads(out) if out.strip() else None
        except ValueError:
            document = None
        return process.returncode, document, out, process.stderr.decode("utf-8", "replace")

    def environment(self, base: Path) -> dict:
        # HOME and every harness folder in the fixture home; PATH without any vidbyte-cli.
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        h = self.home.path
        env.update({
            "HOME": str(h), "USERPROFILE": str(h), "CLAUDE_CONFIG_DIR": str(h / ".claude"), "CODEX_HOME": str(h / ".codex"),
            "XDG_DATA_HOME": str(h / ".local" / "share"), "APPDATA": str(h / "AppData" / "Roaming"), "LOCALAPPDATA": str(h / "AppData" / "Local"),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(base / "vb-state"), "FAKE_CLI_LOG": str(self.fake.log), "PYTHONPATH": str(self.fake.module_dir),
            "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PATH": self.clean_path(),
        })
        return env

    @staticmethod
    def clean_path() -> str:
        # The current PATH minus every folder that holds a vidbyte-cli.
        keep = []
        for folder in os.environ.get("PATH", "").split(os.pathsep):
            if folder and not any(os.path.exists(os.path.join(folder, n)) for n in ("vidbyte-cli", "vidbyte-cli.exe", "vidbyte-cli.cmd", "vidbyte-cli.bat")):
                keep.append(folder)
        return os.pathsep.join(keep)


class SessionBuilder:
    """Builds in-memory sessions turn by turn for detector, scenario, and digest unit tests."""

    @staticmethod
    def make(steps: list, harness: str = "claude-code", sid: str = "s", cwd: str = "/work/app", minutes: float = 0) -> Session:
        # Each step is a prompt string or {"text", "actions", "failed", "interrupt", "say"}; "say" ending in "?" makes the next turn follow a question.
        session = Session(harness, sid, "mem:%s" % sid)
        session.add_cwd(cwd)
        for index, step in enumerate(steps):
            step = {"text": step} if isinstance(step, str) else step
            session.add_turn(step["text"], Clock.parse_ts(Stamp.at(minutes + index)))
            for action in step.get("actions", ()):
                session.note_action(action)
            for _ in range(step.get("failed", 0)):
                session.note_failure()
            if step.get("interrupt"):
                session.note_interrupt()
                continue
            session.note_agent_text(step.get("say", "Done."))
            session.note_turn_end("finished")
        return session.finish()

    @staticmethod
    def signals(previous: dict, text: str) -> tuple:
        # The (signals, strength) of `text` as the reply to one previous step.
        session = SessionBuilder.make([previous, text])
        turns = session.turns
        return CorrectionRules().signals(turns[1].text, turns[1], turns[0])


class UnitTests:
    """Section 10 unit tests."""

    EDIT = {"text": "update the handler", "actions": ["Edit src/handler.py"]}

    def __init__(self, base: Path) -> None:
        # Keeps a scratch folder.
        self.base = base

    def rules_pushback(self) -> None:
        # "no, use the other file" after an Edit is a strong pushback.
        names, strength = SessionBuilder.signals(self.EDIT, "no, use the other file")
        Check.true("pushback" in names, "no pushback: %r" % (names,))
        Check.equal(strength, "strong")

    def rules_long_paste(self) -> None:
        # A 5,000-character pasted log with "no," deep inside is not a correction.
        text = "Here is the build log:\n" + ("compiling module ok\n" * 150) + "no, that's wrong\n" + ("x" * 2000)
        Check.true(len(text) > 5000, "fixture too short")
        Check.equal(SessionBuilder.signals(self.EDIT, text), ((), ""))

    def rules_after_question(self) -> None:
        # A bare "no" after a question is dropped; "no, I meant the API route" is kept and downgraded to medium.
        asked = {"text": "set up the client", "actions": ["Edit client.py"], "say": "Should I use npm for this?"}
        Check.equal(SessionBuilder.signals(asked, "no"), ((), ""))
        names, strength = SessionBuilder.signals(asked, "no, I meant the API route")
        Check.true("pushback" in names, "pushback missing")
        Check.equal(strength, "medium", "after a question")

    def rules_undo_needs_action(self) -> None:
        # "revert the last commit" is an undo only when the previous turn acted.
        Check.equal(SessionBuilder.signals({"text": "explain the diff"}, "revert the last commit"), ((), ""))
        names, strength = SessionBuilder.signals(self.EDIT, "revert the last commit")
        Check.true("undo" in names and strength == "strong", "undo after an edit: %r %r" % (names, strength))

    def rules_interrupt(self) -> None:
        # "continue" after an interruption is a resume; "stop, edit api.py instead" is a redirect with a directive.
        stopped = {"text": "refactor auth", "actions": ["Edit auth.py"], "interrupt": True}
        Check.equal(SessionBuilder.signals(stopped, "continue"), ((), ""))
        names, _ = SessionBuilder.signals(stopped, "stop, edit api.py instead")
        Check.true("interrupt_redirect" in names and "directive" in names, "signals: %r" % (names,))

    def rules_fix_after_failure(self) -> None:
        # A short fix after a mostly failed turn counts; a nudge does not; one failure in a long turn does not.
        failed = {"text": "install deps", "actions": ["Bash: npm install", "Bash: npm install --force"], "failed": 2}
        names, _ = SessionBuilder.signals(failed, "use `pnpm install`")
        Check.true("fix_after_failure" in names, "fix not seen: %r" % (names,))
        Check.equal(SessionBuilder.signals(failed, "try again"), ((), ""))
        long_turn = {"text": "build the feature", "actions": ["Read f%d.py" % i for i in range(30)], "failed": 1}
        Check.equal(SessionBuilder.signals(long_turn, "use pnpm for the build"), ((), ""))

    def rules_questions(self) -> None:
        # "what's wrong with this test?" is a question; "that's wrong" is a correction.
        Check.equal(SessionBuilder.signals(self.EDIT, "what's wrong with this test?"), ((), ""))
        names, _ = SessionBuilder.signals(self.EDIT, "that's wrong")
        Check.true("pushback" in names, "that's wrong missed")

    def rules_not_corrections(self) -> None:
        # "no problem, thanks", a quoted "you didn't", and instructions with a stop or a constraint are not corrections.
        Check.equal(SessionBuilder.signals(self.EDIT, "no problem, thanks"), ((), ""))
        Check.equal(SessionBuilder.signals(self.EDIT, "> you didn't run the migration, said the bot\nplease summarize this thread"), ((), ""))
        Check.equal(SessionBuilder.signals(self.EDIT, "stop the runner and merge"), ((), ""))
        Check.equal(SessionBuilder.signals(self.EDIT, "Add the endpoint. Don't touch the schema."), ((), ""))
        names, _ = SessionBuilder.signals(self.EDIT, "don't touch the schema, just add a column")
        Check.true("directive" in names, "a leading directive was missed")

    def rules_praise_additive_selffix(self) -> None:
        # After an interruption: praise, additive notes, typo fixes, and short questions are not corrections.
        stopped = {"text": "ship the release", "actions": ["Bash: git tag v1"], "interrupt": True}
        for text in ("great, merge into main", "you can merge into main after you are done too", "next pr*", "516 i mean", "and which ones do we already have"):
            Check.equal(SessionBuilder.signals(stopped, text), ((), ""), text)
        names, strength = SessionBuilder.signals(stopped, "great, but you forgot the tests")
        Check.true("pushback" in names and strength == "strong", "praise hid a real correction: %r" % (names,))

    def rules_commands_and_turn0(self) -> None:
        # Turn 0, slash commands, and $skill invocations are never corrections.
        session = SessionBuilder.make(["no, that's wrong", {"text": "/review no, that's wrong", "actions": ["Edit a.py"]}, "$pr-explain no that's wrong"])
        Check.equal(CorrectionDetector().detect([session]), [])

    def detector_templates(self) -> None:
        # A long prompt repeated in two sessions (an expanded slash-command template) is never a correction.
        template = "No, follow the review checklist below instead of guessing: " + " ".join("step%d" % i for i in range(30))
        sessions = [SessionBuilder.make([self.EDIT, template], sid="t%d" % n) for n in range(2)]
        detector = CorrectionDetector()
        Check.equal(detector.detect(sessions), [])
        Check.equal(detector.overall(sessions, [])["templates_skipped"], 2, "templates_skipped")

    def detector_episodes(self) -> None:
        # Corrections at turns 3, 4, and 7 form two episodes; the first one's ask turn is turn 2.
        steps = [self.EDIT, self.EDIT, {"text": "rename the module", "actions": ["Edit mod.py"]},
                 {"text": "no, that's wrong", "actions": ["Edit mod2.py"]}, {"text": "that's wrong again", "actions": ["Edit mod3.py"]},
                 self.EDIT, self.EDIT, "no, revert that"]
        episodes = sorted(CorrectionDetector().detect([SessionBuilder.make(steps)]), key=lambda e: e.first.turn.index)
        Check.equal([[c.turn.index for c in e.corrections] for e in episodes], [[3, 4], [7]])
        Check.equal([e.prompts_lost for e in episodes], [2, 1])
        Check.equal(episodes[0].ask_turn.index, 2, "ask turn")

    def detector_actions_from_previous(self) -> None:
        # The corrected actions come from the previous turn; "after" is what the agent did once corrected.
        session = SessionBuilder.make([{"text": "install", "actions": ["Bash: npm install"]}, {"text": "no, use pnpm", "actions": ["Bash: pnpm install"]}])
        episode = CorrectionDetector().detect([session])[0]
        Check.equal(episode.corrected_actions(), ["Bash: npm install"])
        Check.equal(episode.first.after, ["Bash: pnpm install"])

    def detector_ask_lookback(self) -> None:
        # The ask turn skips bare "continue" prompts back to the real task.
        session = SessionBuilder.make([{"text": "fix the login bug", "actions": ["Edit login.py"]}, {"text": "continue", "actions": ["Edit login.py"]}, "no, that's wrong"])
        episode = CorrectionDetector().detect([session])[0]
        Check.equal(episode.ask_turn.text, "fix the login bug")

    def detector_empty(self) -> None:
        # No turns or one turn yield nothing, and the rate is 0 with no prompts.
        detector = CorrectionDetector()
        sessions = [Session("claude-code", "e", "mem"), SessionBuilder.make(["no, that's wrong"])]
        Check.equal(detector.detect(sessions), [])
        Check.equal(detector.overall([Session("codex", "z", "mem")], [])["per_100_prompts"], 0.0)

    def kinds_table(self) -> None:
        # Each correction phrasing lands in its kind.
        cases = [
            (self.EDIT, "you deleted my migrations", "destructive_action"),
            ({"text": "clean up", "actions": ["Bash: git reset --hard HEAD~3"]}, "no, why did you do that", "destructive_action"),
            ({"text": "run it", "actions": ["Bash: npm run dev"], "interrupt": True}, "continue, tool stalled", "stalled"),
            (self.EDIT, "it's still failing", "false_done"),
            (self.EDIT, "don't commit yet", "ask_first"),
            (self.EDIT, "wrong branch", "wrong_target"),
            (self.EDIT, "no, use pnpm not npm", "wrong_tool"),
            (self.EDIT, "I meant the docs page", "misread_intent"),
            (self.EDIT, "no, I don't like this approach", "rejected_approach"),
            ({"text": "refactor", "actions": ["Edit a.py"], "interrupt": True}, "edit the readme", "off_track"),
        ]
        for previous, text, kind in cases:
            episodes = CorrectionDetector().detect([SessionBuilder.make([previous, text])])
            Check.true(episodes, "no correction for %r" % text)
            Check.equal(MistakeKinds().classify(episodes[0]), kind, text)

    def builder_clusters(self) -> None:
        # Three pnpm corrections in two projects form one scenario; a wrong-branch correction stays apart.
        npm = {"text": "add the package", "actions": ["Bash: npm install left-pad"]}
        sessions = [SessionBuilder.make([npm, "no, use pnpm not npm here"], sid="a", cwd="/w/web"),
                    SessionBuilder.make([npm, "no, use pnpm not npm"], sid="b", cwd="/w/web"),
                    SessionBuilder.make([npm, "no, use pnpm not npm in this repo"], sid="c", cwd="/w/api"),
                    SessionBuilder.make([self.EDIT, "wrong branch, use main"], sid="d", cwd="/w/web")]
        scenarios = ScenarioBuilder().build(CorrectionDetector().detect(sessions))
        Check.equal(sorted(len(s.episodes) for s in scenarios), [1, 3])
        Check.equal({s.kind for s in scenarios}, {"wrong_tool", "wrong_target"})

    def builder_kinds_separate(self) -> None:
        # Identical words with different kinds never share a scenario.
        stopped = {"text": "update docs", "actions": ["Edit docs.md"], "interrupt": True}
        failed = {"text": "update docs", "actions": ["Bash: make docs"], "failed": 1}
        sessions = [SessionBuilder.make([stopped, "use readme.md now"], sid="x"), SessionBuilder.make([failed, "use readme.md now"], sid="y")]
        scenarios = ScenarioBuilder().build(CorrectionDetector().detect(sessions))
        Check.equal(len(scenarios), 2, "scenarios")
        Check.equal(len({s.kind for s in scenarios}), 2, "kinds")

    def builder_ranking(self) -> None:
        # Three sessions outrank three corrections in one session; a destructive singleton gets its bonus.
        npm = {"text": "add the package", "actions": ["Bash: npm install a"]}
        spread = [SessionBuilder.make([npm, "no, use pnpm not npm"], sid="s%d" % n) for n in range(3)]
        branch = {"text": "commit", "actions": ["Bash: git commit"]}
        one = SessionBuilder.make([branch, "wrong branch", "wrong branch again, the main branch", "wrong branch still, use main"], sid="one")
        scenarios = ScenarioBuilder().build(CorrectionDetector().detect(spread + [one]))
        Check.equal(scenarios[0].kind, "wrong_tool", "top scenario")
        builder = ScenarioBuilder()
        plain = builder.build(CorrectionDetector().detect([SessionBuilder.make([self.EDIT, "wrong branch"], sid="p")]))[0]
        boom = builder.build(CorrectionDetector().detect([SessionBuilder.make([self.EDIT, "you deleted my file"], sid="q")]))[0]
        Check.true(boom.score >= plain.score + 3, "no destructive bonus: %r vs %r" % (boom.score, plain.score))
        Check.equal(ScenarioBuilder().build([]), [])

    def inventory(self) -> None:
        # Block-scalar and quoted frontmatter parse, a file without frontmatter uses the folder name, an unreadable entry is skipped, and 60 is the cap.
        root = self.base / "inv-home"
        skills = root / ".claude" / "skills"
        Writer.text(skills / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: >-\n  Use when the alpha\n  thing happens.\n---\n")
        Writer.text(skills / "beta" / "SKILL.md", "---\nname: \"beta\"\ndescription: 'Use when beta: quoted.'\n---\n")
        Writer.text(skills / "gamma" / "SKILL.md", "# no frontmatter\n")
        (skills / "delta" / "SKILL.md").mkdir(parents=True, exist_ok=True)
        found = {e["name"]: e for e in SkillInventory(root, [root / ".claude"]).scan([])}
        Check.equal(found["alpha"]["description"], "Use when the alpha thing happens.")
        Check.equal(found["beta"]["description"], "Use when beta: quoted.")
        Check.true("gamma" in found and found["gamma"]["description"] == "", "folder-name fallback")
        Check.true("delta" not in found, "unreadable SKILL.md was listed")
        for n in range(70):
            Writer.text(root / ".agents" / "skills" / ("s%02d" % n) / "SKILL.md", "---\nname: s%02d\ndescription: Use when %d.\n---\n" % (n, n))
        Check.equal(len(SkillInventory(root, [root / ".claude"]).scan([])), 60, "cap")

    def many_sessions(self, count: int) -> list:
        # count sessions, each with one long, distinct pushback correction.
        sessions = []
        for n in range(count):
            words = " ".join("word%dx%d" % (n, k) for k in range(60))
            text = "no, that's wrong, use tool%d instead of tool%d in module%d; %s; my key is %s" % (n, n + 100, n, words, SECRET_KEY)
            sessions.append(SessionBuilder.make([{"text": "build part %d of the importer" % n, "actions": ["Edit part%d.py" % n] * 5}, text], sid="m%d" % n, cwd="/w/proj%d" % (n % 3), minutes=n * 10))
        return sessions

    def write_digest(self, folder: str, sessions: list, max_chars: int, max_scenarios: int = 12) -> tuple:
        # Runs detection, clustering, and DigestWriter into base/folder; returns (out_dir, written, scenarios).
        out = self.base / folder
        out.mkdir(parents=True, exist_ok=True)
        detector = CorrectionDetector()
        episodes = detector.detect(sessions)
        scenarios = ScenarioBuilder().build(episodes)
        summary = {"since": None, "until": Clock.iso(Clock.now()), "max_sessions": 500, "read": []}
        written = DigestWriter(out, Redactor(Path.home()), max_chars, max_scenarios, FOCUS).write(scenarios, detector.overall(sessions, episodes), [], summary)
        return out, written, scenarios

    def digest_budget(self) -> None:
        # The digest fits max_chars and every full scenario keeps at least one example.
        out, written, _scenarios = self.write_digest("digest-budget", self.many_sessions(30), 20000)
        text = (out / "digest.md").read_text(encoding="utf-8")
        Check.true(len(text) <= 20000, "digest has %d characters" % len(text))
        for tag in written["full"]:
            block = text.split("#### [%s]" % tag, 1)[1].split("####", 1)[0]
            Check.true(re.search(r"^- \[P\d+\] \[S\d+\]", block, re.M), "%s lost every example" % tag)

    def digest_secrets_and_tags(self) -> None:
        # No secret reaches any file, every tag resolves in corrections.json, P tags are unique, and mistakes values carry tags.
        out, _written, _ = self.write_digest("digest-tags", self.many_sessions(14), 60000)
        index = json.loads((out / "corrections.json").read_text(encoding="utf-8"))
        known = {e["tag"] for key in ("scenarios", "sessions", "prompts") for e in index[key]}
        prompt_tags = [p["tag"] for p in index["prompts"]]
        Check.equal(len(prompt_tags), len(set(prompt_tags)), "unique prompt tags")
        for name in ("digest.md", "inputs.json", "corrections.json"):
            text = (out / name).read_text(encoding="utf-8")
            Check.true(SECRET_KEY not in text, "secret in %s" % name)
            if name != "corrections.json":
                for group in TAG_RE.findall(text):
                    for tag in re.split(r"[ ,]+", group):
                        Check.true(tag in known, "%s cites unknown tag %s" % (name, tag))
        inputs = json.loads((out / "inputs.json").read_text(encoding="utf-8"))
        Check.true(0 < len(inputs["mistakes"]) <= 8, "mistakes count %d" % len(inputs["mistakes"]))
        Check.true(all(v.startswith("[C") for v in inputs["mistakes"]), "untagged mistakes value")
        Check.equal((out / FOCUS.name).read_bytes(), FOCUS.read_bytes(), "focus copy")

    def digest_bounds(self) -> None:
        # max_chars below 20,000 and a focus file over 20,000 bytes are refused; the real focus file fits.
        Check.true(FOCUS.stat().st_size <= 20000, "focus file too large")
        try:
            DigestWriter(self.base, Redactor(Path.home()), 19999, 12, FOCUS)
            raise AssertionError("19,999 accepted")
        except ValueError:
            pass
        big = Writer.text(self.base / "big-focus.md", "x" * 20001)
        out = self.base / "digest-bigfocus"
        out.mkdir()
        sessions = self.many_sessions(2)
        episodes = CorrectionDetector().detect(sessions)
        try:
            DigestWriter(out, Redactor(Path.home()), 30000, 12, big).write(ScenarioBuilder().build(episodes), CorrectionDetector().overall(sessions, episodes), [], {"until": None})
            raise AssertionError("oversized focus file accepted")
        except ValueError:
            pass

    def config_count_argv(self) -> None:
        # count is clamped to 2..12; the argv has root options first, the four categories, both files, the cap, and --dry-run/--project only when asked.
        config = CorrectionSuggestConfig()
        Check.equal(config.count({"scenarios": [{"tag": "C1", "full": True}]}), 2)
        Check.equal(config.count({"scenarios": [{"tag": "C%d" % n, "full": True} for n in range(15)]}), 12)
        inputs = {"goal": "g", "values": {"mistakes": ["[C1] m"]}, "project": None}
        index = {"scenarios": [{"tag": "C1", "full": True}, {"tag": "C2", "full": True}, {"tag": "C3", "full": True}]}
        argv = config.argv("vb", inputs, index, True)
        Check.equal(argv[:6], ["vb", "--json", "--no-input", "agents", "suggest", "run"])
        Check.equal([argv[i + 1] for i, a in enumerate(argv) if a == "--category"], ["risk_prevention", "leverage", "learning", "verification"])
        Check.equal(argv[argv.index("--count") + 1], "3")
        Check.equal(argv[argv.index("--horizon") + 1], "any")
        Check.equal(argv[argv.index("--files") + 1], "digest.md")
        Check.equal(argv[argv.index("--instructions") + 1], "skills-from-corrections.md")
        Check.equal(argv[-3:], ["--max-total-tokens", "400000", "--dry-run"])
        Check.true("--project" not in argv, "--project without a project")
        paid = config.argv("vb", dict(inputs, project="my-proj"), index, False)
        Check.true("--dry-run" not in paid and paid[paid.index("--project") + 1] == "my-proj", "paid argv: %r" % paid)

    def config_validator(self) -> None:
        # Unknown tags, untagged mistakes, and no mistakes are problems; [all] and [setup] are accepted.
        config = CorrectionSuggestConfig()
        index = {"scenarios": [{"tag": "C1"}], "sessions": [{"tag": "S1"}], "prompts": [{"tag": "P1"}]}
        validator = config.validator(index, Redactor(Path.home()))
        good = {"goal": "Propose skills.", "outcomes": ["[all] 3 corrections."], "mistakes": ["[C1 P1] wrong tool."], "context": ["[setup] no skills.", "[S1] one session."], "project": None}
        _inputs, problems = validator.validate(good)
        Check.equal(problems, [], "good inputs")
        for bad in (dict(good, mistakes=["[C99] unknown"]), dict(good, mistakes=["untagged value"]), dict(good, mistakes=[])):
            _inputs, problems = validator.validate(bad)
            Check.true(problems, "no problem for %r" % bad["mistakes"])

    def parser_cases(self) -> None:
        # A well-formed idea parses fully; a title without a slug derives one with a problem; bad slugs and descriptions are flagged.
        parser = SkillIdeaParser()
        idea = {"title": "`use-pnpm-in-web`: Use pnpm in the web repo [C1]", "summary": "Description: Use when installing packages in web. Got wrong: it ran npm [P2]. Correct: use pnpm.",
                "suggested_actions": ["Rule: Always use pnpm.", "Step: Run pnpm install.", "1. Step: Run the tests."], "considerations": ["Where: Claude Code, web", "Eval: given [P2], pnpm is used"],
                "completion_criteria": "Given [P2], no correction."}
        spec = parser.parse(idea)
        Check.equal((spec.slug, spec.slug_source), ("use-pnpm-in-web", "title"))
        Check.equal(spec.title, "Use pnpm in the web repo")
        Check.equal(spec.description, "Use when installing packages in web.")
        Check.equal(spec.got_wrong, "it ran npm [P2].")
        Check.equal(spec.correct, "use pnpm.")
        Check.equal((spec.rules, spec.steps), (["Always use pnpm."], ["Run pnpm install.", "Run the tests."]))
        Check.equal(spec.problems, [])
        derived = parser.parse({"title": "Run Tests Before Done", "summary": "Use when fixing tests. It claimed done."})
        Check.equal(derived.slug, "run-tests-before-done")
        Check.true(any("derived" in p for p in derived.problems) and any("Got wrong" in p for p in derived.problems), "problems: %r" % derived.problems)
        Check.true(parser.slug_problems("ask-claude-first", "title"), "reserved word accepted")
        Check.true(parser.slug_problems("a" * 65, "title"), "65 characters accepted")
        Check.true(parser.description_problems("Use when " + "x" * 1020), "1,029 characters accepted")
        Check.true(parser.description_problems("Use when <tag> appears"), "angle bracket accepted")
        Check.true(parser.description_problems("When editing files"), "no 'Use when' accepted")


class IntegrationTests:
    """Section 10 integration tests: corrections.py in subprocesses against the fixture homes."""

    def __init__(self, base: Path, fake: FakeCli) -> None:
        # Builds the main home and its runner; other homes are built by the tests that need them.
        self.base = base
        self.fake = fake
        self.home = Home(base / "home-main", "main")
        self.runner = Runner(self.home, fake, base)
        self.out = base / "out-main"
        self.summary = None

    def gather_end_to_end(self) -> None:
        # gather finds 5 corrections in 4 episodes and 3 scenarios, writes the four files, and lists both existing skills.
        code, doc, out, err = self.runner.run("gather", "--out", str(self.out))
        Check.equal(code, 0, "exit (%s %s)" % (out[-400:], err[-400:]))
        self.summary = doc
        c = doc["corrections"]
        Check.equal((c["corrections"], c["episodes"], c["sessions_with_corrections"]), (5, 4, 4), "corrections, episodes, sessions")
        Check.equal(doc["sessions"]["kept_by_harness"], {"claude-code": 2, "codex": 2})
        kinds = [(s["tag"], s["kind"], s["sessions"]) for s in doc["scenarios"]]
        Check.equal(kinds, [("C1", "wrong_tool", 2), ("C2", "false_done", 1), ("C3", "off_track", 1)])
        Check.equal(doc["count"], 3)
        for name in ("digest.md", "corrections.json", "inputs.json", "skills-from-corrections.md"):
            Check.true((self.out / name).is_file(), "%s missing" % name)
        names = {s["name"] for s in doc["skills_found"]}
        Check.equal(names, {"deploy-app", "web-style"}, "skills found")

    def gather_no_secrets(self) -> None:
        # The secret and the email reach no file; the home path appears only in corrections.json's local session fields.
        home = str(self.home.path)
        variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
        for name in ("digest.md", "inputs.json", "corrections.json"):
            text = (self.out / name).read_text(encoding="utf-8")
            Check.true(SECRET_KEY not in text and SECRET_EMAIL not in text and "bob.builder" not in text, "secret or email part in %s" % name)
            if name != "corrections.json":
                Check.true(not any(v in text for v in variants), "home path in %s" % name)
        header = next(line for line in (self.out / "digest.md").read_text(encoding="utf-8").splitlines() if line.startswith("#### [C1]"))
        Check.true("REDACTED" not in header and "abcdefghij" not in header, "a secret became a scenario term: %r" % header)
        index = json.loads((self.out / "corrections.json").read_text(encoding="utf-8"))
        stripped = dict(index, sessions=[{k: v for k, v in s.items() if k not in ("cwd", "source")} for s in index["sessions"]], skills_found=[], read=[])
        Check.true(not any(v in json.dumps(stripped) for v in variants | {home.replace("\\", "\\\\")}), "home path outside the local session fields")

    def gather_tags(self) -> None:
        # Every tag in digest.md and inputs.json resolves in corrections.json, and the C1 block quotes the pnpm correction.
        index = json.loads((self.out / "corrections.json").read_text(encoding="utf-8"))
        known = {e["tag"] for key in ("scenarios", "sessions", "prompts") for e in index[key]}
        for name in ("digest.md", "inputs.json"):
            for group in TAG_RE.findall((self.out / name).read_text(encoding="utf-8")):
                for tag in re.split(r"[ ,]+", group):
                    Check.true(tag in known, "%s cites unknown %s" % (name, tag))
        digest = (self.out / "digest.md").read_text(encoding="utf-8")
        Check.true("use pnpm not npm" in digest and "Bash: npm install lodash" in digest, "C1 example lost its correction or actions")
        Check.true("- 1 correction in 1 episode, 1 session, 1 project;" in digest, "singular counts in the C3 header")

    def gather_nothing(self) -> None:
        # No corrections exits 3 with no_corrections; every harness disabled exits 3 with no_sessions.
        calm = Runner(Home(self.base / "home-calm", "calm"), self.fake, self.base)
        code, doc, _out, _err = calm.run("gather")
        Check.equal((code, doc and doc.get("reason")), (3, "no_corrections"))
        code, doc, _out, _err = calm.run("gather", "--no-claude", "--no-codex", "--no-opencode", "--no-gemini", "--no-qwen", "--no-openclaude", "--no-factory", "--no-pi")
        Check.equal((code, doc and doc.get("reason")), (3, "no_sessions"))
        code, doc, _out, _err = calm.run("gather", "--only", "pi")
        Check.equal((code, doc and doc.get("reason")), (3, "no_sessions"), "--only pi on a home with only Claude Code")

    def gather_more_harnesses(self) -> None:
        # OpenClaude, Factory Droid, and Pi sessions are read; their corrections count; --only keeps one harness and overrides --no-*.
        runner = Runner(Home(self.base / "home-more", "moreharness"), self.fake, self.base)
        out = self.base / "out-more"
        code, doc, text, err = runner.run("gather", "--out", str(out))
        Check.equal(code, 0, "exit (%s %s)" % (text[-300:], err[-300:]))
        Check.equal(doc["sessions"]["kept_by_harness"], {"openclaude": 1, "factory-droid": 1, "pi": 1}, "kept_by_harness")
        index = json.loads((out / "corrections.json").read_text(encoding="utf-8"))
        corrected = {s["harness"] for s in index["sessions"] if s["tag"] in {p["session"] for p in index["prompts"] if p["role"] == "correction"}}
        Check.equal(corrected, {"openclaude", "pi"}, "harnesses with a correction")
        pi_prompt = next(p for p in index["prompts"] if "list app/ instead" in p["text"])
        Check.true(pi_prompt["text"].startswith("no, list app/ instead"), "Pi correction text: %r" % pi_prompt["text"])
        Check.true("web-install" in {s["name"] for s in doc["skills_found"]}, "OpenClaude skill not listed")
        goal = json.loads((out / "inputs.json").read_text(encoding="utf-8"))["goal"]
        Check.true("OpenClaude" in goal and "Pi" in goal and "Factory Droid" in goal, "goal names: %r" % goal[:200])
        code, doc, _text, _err = runner.run("gather", "--only", "pi", "--no-pi", "--out", str(self.base / "out-more-pi"))
        Check.equal((code, doc["sessions"]["kept_by_harness"]), (0, {"pi": 1}), "--only pi --no-pi")

    def gather_bad_opencode(self) -> None:
        # A non-SQLite opencode.db shows as a read[] entry with status error, and the Claude Code session is still read.
        runner = Runner(Home(self.base / "home-badoc", "badopencode"), self.fake, self.base)
        code, doc, out, _err = runner.run("gather", "--out", str(self.base / "out-badoc"))
        Check.equal(code, 0, "exit (%s)" % out[-300:])
        entry = next(e for e in doc["sessions"]["read"] if e["harness"] == "opencode")
        Check.equal(entry["status"], "error", "opencode read[] status")
        Check.true("opencode.db not read" in entry.get("error", ""), "no error text: %r" % entry)
        Check.equal(doc["corrections"]["corrections"], 1, "the Claude Code correction")

    def gather_bad_out_and_flags(self) -> None:
        # A non-empty --out and an out-of-range --max-chars are usage errors (exit 2).
        busy = self.base / "busy"
        Writer.text(busy / "keep.txt", "x")
        code, _doc, _out, _err = self.runner.run("gather", "--out", str(busy))
        Check.equal(code, 2, "non-empty --out")
        code, _doc, _out, _err = self.runner.run("gather", "--max-chars", "1000")
        Check.equal(code, 2, "--max-chars 1000")

    def suggest_dry_run(self) -> None:
        # The dry run calls the fake once, from inside out_dir, with relative files and --dry-run, and writes no paid-run.txt.
        code, doc, out, err = self.runner.run("suggest", "--out", str(self.out), "--dry-run", "--cli", self.fake.path)
        Check.equal(code, 0, "exit (%s %s)" % (out[-300:], err[-300:]))
        Check.equal((doc["stop_reason"], doc["mode"]), ("dry_run", "dry_run"))
        call = self.fake.calls()[-1]
        argv = call["argv"]
        Check.equal(os.path.normcase(os.path.realpath(call["cwd"])), os.path.normcase(os.path.realpath(str(self.out))), "cwd")
        Check.equal(argv[:5], ["--json", "--no-input", "agents", "suggest", "run"])
        Check.equal((argv[argv.index("--files") + 1], argv[argv.index("--instructions") + 1]), ("digest.md", "skills-from-corrections.md"))
        Check.equal((argv[argv.index("--count") + 1], argv[argv.index("--horizon") + 1]), ("3", "any"))
        Check.equal(argv[-3:], ["--max-total-tokens", "400000", "--dry-run"])
        inputs = json.loads((self.out / "inputs.json").read_text(encoding="utf-8"))
        Check.equal([argv[i + 1] for i, a in enumerate(argv) if a == "--mistakes"], inputs["mistakes"], "mistakes values")
        Check.true(not (self.out / "paid-run.txt").exists(), "dry run wrote paid-run.txt")

    def suggest_bad_inputs(self) -> None:
        # An untagged mistakes value is refused with exit 2 before anything runs.
        folder = self.base / "out-bad-inputs"
        shutil.copytree(str(self.out), str(folder))
        inputs = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
        inputs["mistakes"] = ["no tag here"]
        Writer.text(folder / "inputs.json", json.dumps(inputs))
        before = len(self.fake.calls())
        code, doc, _out, _err = self.runner.run("suggest", "--out", str(folder), "--dry-run", "--cli", self.fake.path)
        Check.equal(code, 2)
        Check.true(doc and doc.get("problems"), "no problems listed")
        Check.equal(len(self.fake.calls()), before, "the fake was called")

    def result_document(self) -> dict:
        # A suggestions.result built from this run's real tags: grounded, ungrounded, unknown-tag, and duplicate-slug ideas.
        index = json.loads((self.out / "corrections.json").read_text(encoding="utf-8"))
        c1 = next(s for s in index["scenarios"] if s["tag"] == "C1")
        p_a, p_b = c1["correction_prompts"][:2]
        ideas = [
            {"id": "idea-001", "rank": 1, "title": "`use-pnpm-in-web`: Use pnpm, not npm, in the web repo",
             "summary": "Description: Use when installing packages or running scripts in the web repo: pnpm only. Got wrong: it ran npm install and the user said 'use pnpm not npm' [C1 %s %s]. Correct: use pnpm add." % (p_a, p_b),
             "suggested_actions": ["Rule: Use pnpm for every install in the web repo.", "Step: Run pnpm install before any script.", "Step: Never run npm install."],
             "why_now": "[C1] 2 corrections in 2 sessions.", "considerations": ["Where: Claude Code; project web; package.json", "Trigger: 'add the X dependency'", "Do not fire: other repositories"],
             "completion_criteria": "Given the request in [%s], the agent uses pnpm." % p_a, "first_action": "Create `.claude/skills/use-pnpm-in-web/SKILL.md`.", "horizon": "now", "primary_category": "learning"},
            {"id": "idea-002", "rank": 2, "title": "Be careful", "summary": "General advice.", "horizon": "later", "primary_category": "learning"},
            {"id": "idea-003", "rank": 3, "title": "`run-tests-before-done`: Run the tests before saying done",
             "summary": "Description: Use when fixing a failing test. Got wrong: it claimed the test passed [C2] [P999]. Correct: run pytest first.",
             "suggested_actions": ["Rule: Run pytest before saying done."], "horizon": "now", "primary_category": "verification"},
            {"id": "idea-004", "rank": 4, "title": "`use-pnpm-in-web`: Duplicate", "summary": "Description: Use when adding packages. Got wrong: [C1]. Correct: pnpm.", "horizon": "next", "primary_category": "leverage"},
        ]
        data = {"status": "complete", "stop_reason": "completed", "settings": {"model": "gpt-6-sol", "critic_model": "gpt-6-sol"}, "ideas": ideas,
                "usage": {"model_calls": 5, "input_tokens": 200000, "cached_input_tokens": 50000, "output_tokens": 40000}, "context_manifest": [], "warnings": []}
        return {"schema_version": 1, "kind": "suggestions.result", "data": data}

    def suggest_paid_once(self) -> None:
        # The paid run writes paid-run.txt and result.json and reports usage and cost; a second paid run exits 2 without a call.
        result_path = Writer.text(self.base / "fake-result.json", json.dumps(self.result_document()))
        env = {"FAKE_CLI_RESULT": str(result_path)}
        code, doc, out, err = self.runner.run("suggest", "--out", str(self.out), "--paid", "--cli", self.fake.path, extra_env=env)
        Check.equal(code, 0, "exit (%s %s)" % (out[-300:], err[-300:]))
        Check.true((self.out / "paid-run.txt").is_file() and (self.out / "result.json").is_file(), "paid files missing")
        Check.equal((doc["ideas"], doc["usage"]["model_calls"]), (4, 5))
        Check.true(doc["estimated_cost_usd"] and doc["estimated_cost_usd"] > 0, "no cost estimate")
        paid_calls = [c for c in self.fake.calls() if "run" in c["argv"] and "--dry-run" not in c["argv"]]
        code, _doc, _out, _err = self.runner.run("suggest", "--out", str(self.out), "--paid", "--cli", self.fake.path, extra_env=env)
        Check.equal(code, 2, "second paid run")
        Check.equal(len([c for c in self.fake.calls() if "run" in c["argv"] and "--dry-run" not in c["argv"]]), len(paid_calls), "a second paid call was made")

    def suggest_cli_error(self) -> None:
        # The fake exits 5 with an error JSON on stderr: the report carries error.code and the exit is 5.
        folder = self.base / "out-credits"
        code, _doc, _out, _err = self.runner.run("gather", "--out", str(folder))
        Check.equal(code, 0)
        code, doc, _out, _err = self.runner.run("suggest", "--out", str(folder), "--paid", "--cli", self.fake.path, extra_env={"FAKE_CLI_MODE": "credits"})
        Check.equal(code, 5)
        Check.equal(doc["error"]["code"], "insufficient_credits")

    def suggest_no_cli(self) -> None:
        # Without --cli and with no vidbyte-cli on PATH, suggest and feedback exit 127 and run nothing.
        folder = self.base / "out-nocli"
        self.runner.run("gather", "--out", str(folder))
        before = len(self.fake.calls())
        code, doc, _out, _err = self.runner.run("suggest", "--out", str(folder), "--dry-run")
        Check.equal(code, 127)
        Check.true("setup.md" in doc["error"], "no setup pointer")
        Writer.text(folder / "result.json", json.dumps(self.result_document()))
        code, _doc, _out, _err = self.runner.run("feedback", "--out", str(folder), "--idea", "idea-001", "--accept")
        Check.equal(code, 127, "feedback without a CLI")
        Check.equal(len(self.fake.calls()), before, "something ran")

    def report_after_paid(self) -> None:
        # report grounds idea-001 with its quoted correction, flags the ungrounded, unknown-tag, and duplicate ideas, and lists C3 as uncovered.
        code, doc, out, err = self.runner.run("report", "--out", str(self.out))
        Check.equal(code, 0, "exit (%s %s)" % (out[-300:], err[-300:]))
        ideas = {i["id"]: i for i in doc["ideas"]}
        Check.true(ideas["idea-001"]["grounded"] and ideas["idea-001"]["scenarios"] == ["C1"], "idea-001: %r" % ideas["idea-001"])
        Check.equal(ideas["idea-001"]["how_often"]["corrections"], 2, "how often")
        Check.equal(ideas["idea-002"]["grounded"], False)
        Check.equal(ideas["idea-003"]["unknown_tags"], ["P999"])
        Check.true(any("same slug" in p for p in ideas["idea-004"]["problems"]), "duplicate slug not flagged")
        Check.equal([s["tag"] for s in doc["uncovered_scenarios"]], ["C3"])
        report = (self.out / "report.md").read_text(encoding="utf-8")
        Check.true("use pnpm not npm" in report, "report.md lost the quoted correction")
        sections = report.split("# Without evidence from your sessions")
        Check.true(len(sections) == 2 and "(idea-002)" in sections[1] and "(idea-002)" not in sections[0], "ungrounded idea not set apart")
        Check.true("- Trigger: 'add the X dependency'" in report and "- Do not fire: other repositories" in report, "where-it-applies labels lost")
        Check.true(SECRET_KEY not in report and SECRET_EMAIL not in report, "secret in report.md")

    def scaffold_draft_and_write(self) -> None:
        # The draft has valid frontmatter, a quoted description, and no tags; --write writes once and refuses to overwrite or write an ungrounded idea.
        code, doc, out, _err = self.runner.run("scaffold", "--out", str(self.out), "--idea", "idea-001")
        Check.equal(code, 0, "exit (%s)" % out[-300:])
        text = doc["text"]
        Check.true(text.startswith("---\nname: use-pnpm-in-web\ndescription: \"Use when installing packages or running scripts in the web repo: pnpm only.\"\n---\n"), "frontmatter: %r" % text[:200])
        Check.true(not re.search(r"\[[CPS]\d+", text), "evidence tags left in the draft")
        Check.true("## Rules" in text and "## Steps" in text and "## Where it applies" in text, "draft sections")
        Check.true("Given the request in an earlier session, the agent uses pnpm." in text, "a cited prompt left a gap: %r" % text)
        Check.true("## Do this instead\n\nUse pnpm add." in text, "section text not capitalized")
        Check.true(re.search(r"<!-- Drafted by turn-corrections-into-skills on \d{4}-\d{2}-\d{2} from 2 corrections in 2 sessions \(idea-001\)\.", text), "draft comment: %r" % text[-200:])
        Check.true(Path(doc["draft_path"]).is_file() and "drafts" in doc["draft_path"], "draft not saved in out_dir")
        Check.true(doc["destinations"] and all("path" in d for d in doc["destinations"]), "no destinations")
        dest = self.base / "proj" / ".claude" / "skills"
        code, doc, out, _err = self.runner.run("scaffold", "--out", str(self.out), "--idea", "idea-001", "--dest", str(dest), "--write")
        Check.equal(code, 0, "write (%s)" % out[-300:])
        target = dest / "use-pnpm-in-web" / "SKILL.md"
        Check.true(target.is_file(), "SKILL.md not written")
        Writer.text(target, "user edits\n")
        code, _doc, _out, _err = self.runner.run("scaffold", "--out", str(self.out), "--idea", "idea-001", "--dest", str(dest), "--write")
        Check.equal(code, 2, "overwrite")
        Check.equal(target.read_text(encoding="utf-8"), "user edits\n", "the existing file changed")
        code, _doc, _out, _err = self.runner.run("scaffold", "--out", str(self.out), "--idea", "idea-002", "--dest", str(dest), "--write")
        Check.equal(code, 2, "ungrounded idea written")
        code, _doc, _out, _err = self.runner.run("scaffold", "--out", str(self.out), "--idea", "idea-001", "--write")
        Check.equal(code, 2, "--write without --dest")
        store = self.home.path / ".pi" / "agent" / "skills"
        code, _doc, _out, _err = self.runner.run("scaffold", "--out", str(self.out), "--idea", "idea-001", "--dest", str(store), "--write")
        Check.equal((code, (store / "use-pnpm-in-web").exists()), (2, False), "write into Pi's session store")

    def feedback_reject(self) -> None:
        # feedback sends the exact title, the project from inputs.json, and a redacted reason; an unknown idea exits 2.
        inputs = json.loads((self.out / "inputs.json").read_text(encoding="utf-8"))
        inputs["project"] = "my-skills"
        Writer.text(self.out / "inputs.json", json.dumps(inputs))
        code, doc, out, _err = self.runner.run("feedback", "--out", str(self.out), "--idea", "idea-003", "--reject", "--reason", "too broad, key %s" % SECRET_KEY, "--cli", self.fake.path)
        Check.equal(code, 0, "exit (%s)" % out[-300:])
        argv = self.fake.calls()[-1]["argv"]
        Check.equal(argv[:6], ["--json", "agents", "suggest", "feedback", "reject", "--project"])
        Check.equal(argv[argv.index("--project") + 1], "my-skills")
        Check.equal(argv[argv.index("--suggestion") + 1], "`run-tests-before-done`: Run the tests before saying done")
        Check.true(SECRET_KEY not in " ".join(argv), "reason not redacted")
        code, _doc, _out, _err = self.runner.run("feedback", "--out", str(self.out), "--idea", "idea-404", "--accept", "--cli", self.fake.path)
        Check.equal(code, 2, "unknown idea")


class Suite:
    """Builds the fixtures once and runs every test in order."""

    def run(self) -> int:
        # Runs the unit tests, then the integration tests that share one gather output.
        results = Results()
        base = Path(tempfile.mkdtemp(prefix="tcs-test-"))
        try:
            units = UnitTests(base)
            for name, category in self.unit_cases():
                results.run(name, category, getattr(units, name))
            integration = IntegrationTests(base, FakeCli(base))
            for name, category in self.integration_cases():
                results.run(name, category, getattr(integration, name))
        finally:
            shutil.rmtree(str(base), ignore_errors=True)
        return results.finish()

    @staticmethod
    def unit_cases() -> list:
        # (method name, failure category) for every unit test.
        return [
            ("rules_pushback", "Edge Case"), ("rules_long_paste", "Silent Failure"), ("rules_after_question", "Hidden Assumption"),
            ("rules_undo_needs_action", "Hidden Assumption"), ("rules_interrupt", "Edge Case"), ("rules_fix_after_failure", "Silent Failure"),
            ("rules_questions", "Silent Failure"), ("rules_not_corrections", "Silent Failure"), ("rules_praise_additive_selffix", "Silent Failure"),
            ("rules_commands_and_turn0", "Hidden Assumption"), ("detector_templates", "Silent Failure"), ("detector_episodes", "Silent Failure"),
            ("detector_actions_from_previous", "Silent Failure"), ("detector_ask_lookback", "Hidden Assumption"), ("detector_empty", "Edge Case"),
            ("kinds_table", "Silent Failure"), ("builder_clusters", "Silent Failure"), ("builder_kinds_separate", "Hidden Assumption"),
            ("builder_ranking", "Silent Failure"), ("inventory", "Hidden Failure"), ("digest_budget", "Silent Failure"),
            ("digest_secrets_and_tags", "Hidden Failure"), ("digest_bounds", "Edge Case"), ("config_count_argv", "Silent Failure"),
            ("config_validator", "Edge Case"), ("parser_cases", "Silent Failure"),
        ]

    @staticmethod
    def integration_cases() -> list:
        # (method name, failure category) for every integration test, in dependency order.
        return [
            ("gather_end_to_end", "Hidden Failure"), ("gather_no_secrets", "Silent Failure"), ("gather_tags", "Hidden Failure"),
            ("gather_nothing", "Edge Case"), ("gather_more_harnesses", "Hidden Assumption"), ("gather_bad_opencode", "Hidden Failure"),
            ("gather_bad_out_and_flags", "Edge Case"),
            ("suggest_dry_run", "Silent Failure"), ("suggest_bad_inputs", "Hidden Assumption"), ("suggest_paid_once", "Silent Failure"),
            ("suggest_cli_error", "Hidden Failure"), ("suggest_no_cli", "Hidden Assumption"), ("report_after_paid", "Silent Failure"),
            ("scaffold_draft_and_write", "Hidden Failure"), ("feedback_reject", "Silent Failure"),
        ]


if __name__ == "__main__":
    sys.exit(Suite().run())
