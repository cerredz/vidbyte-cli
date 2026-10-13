#!/usr/bin/env python3
"""Verification script for skills/improve-my-prompting (design doc section 10).

Builds synthetic Claude Code, Codex, OpenCode, Gemini CLI, and Qwen Code stores in a temporary
home, imports the skill's modules for unit tests, and runs prompt_review.py in subprocesses whose
HOME, harness folders, and PATH all point into that home. Every suggest and feedback call goes to
a recording fake vidbyte-cli passed with --cli; PATH holds no vidbyte-cli, so an ignored --cli
fails with 127 instead of reaching a real (billing) CLI. Run it with every Python you support:

    python scripts/test-improve-my-prompting.py
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "improve-my-prompting"
SCRIPTS = SKILL / "scripts"
ENTRY = SCRIPTS / "prompt_review.py"
sys.path.insert(0, str(SCRIPTS))

from sessionkit.collector import CollectOptions, SessionCollector  # noqa: E402
from sessionkit.model import Clock, Session  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402
from sessionkit.runner import CostEstimator, RunReporter  # noqa: E402
from sessionkit.claude_code import ClaudeCodeReader  # noqa: E402
from sessionkit.codex import CodexReader  # noqa: E402
from sessionkit.opencode import OpenCodeReader  # noqa: E402
from sessionkit.gemini import GeminiReader  # noqa: E402
from sessionkit.message_logs import FactoryReader, PiReader  # noqa: E402
from sessionkit.model import ActionText  # noqa: E402
from prompting.digest import DigestWriter, TagBook  # noqa: E402
from prompting.report import IdeaReporter, RubricIndex  # noqa: E402
from prompting.signals import SignalCatalog, SignalDetector, TemplateIndex  # noqa: E402
from prompting.suggest import CATEGORIES, PromptingRun  # noqa: E402

SECRET_KEY = "sk-abcdefghijklmnopqrstuvwx1234"
SECRET_GH = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
SECRET_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
SECRET_EMAIL = "bob.builder@example.com"
STRIP_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "VIDBYTE_API_KEY", "PYTHONPATH")
FAKE_CLI_SOURCE = r'''
import json, os, sys

def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    code = int(os.environ.get("FAKE_CLI_EXIT", "0") or 0)
    if code:
        sys.stderr.write("working...\n")
        sys.stderr.write(json.dumps({"kind": "error", "data": {"code": "insufficient_credits", "message": "Out of credits.", "hint": "Top up first.", "retryable": False}}) + "\n")
        return code
    if argv[:6] == ["--json", "--no-input", "agents", "suggest", "run", "--goal"]:
        dry = "--dry-run" in argv
        ideas = [] if dry else [
            {"id": "idea-001", "rank": 1, "title": "State the done-check in your first message", "summary": "In [P1] and [S1] you opened with no check; rubric RC3 and RT3 apply.", "first_action": "Before: 'fix this' -> After: 'fix the failing test in src/app.py; done when pytest passes'", "completion_criteria": "Fewer than 1 in 10 openers lack a check.", "horizon": "now", "primary_category": "quick_wins"},
            {"id": "idea-002", "rank": 2, "title": "Be more specific", "summary": "General advice with no tags.", "horizon": "later", "primary_category": "learning"},
            {"id": "idea-003", "rank": 3, "title": "Cite a missing prompt", "summary": "See [P999] and RZ9 and RX9.", "horizon": "next", "primary_category": "learning"},
        ]
        usage = {"model_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0} if dry else {"model_calls": 5, "input_tokens": 300000, "cached_input_tokens": 100000, "output_tokens": 50000}
        data = {"status": "no_suggestions" if dry else "complete", "stop_reason": "dry_run" if dry else "completed", "settings": {"model": "gpt-6-sol", "critic_model": "gpt-6-sol"}, "usage": usage, "ideas": ideas, "context_manifest": [{"ref": "ctx-001", "kind": "file", "status": "included"}], "warnings": []}
        print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": data}))
        return 0
    print(json.dumps({"schema_version": 1, "kind": "ok", "data": {}}))
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
            detail = str(error) or type(error).__name__
            print("FAIL [%s] %s -- %s" % (category, name, detail))
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
    """Fixture timestamps relative to now, in the formats each harness writes."""

    BASE = time.time() - 2 * 86400

    @staticmethod
    def at(minutes: float) -> float:
        # Epoch seconds `minutes` after the fixture base time.
        return Stamp.BASE + minutes * 60

    @staticmethod
    def iso(minutes: float) -> str:
        # An ISO 8601 UTC string with milliseconds and a Z, like Claude Code and Codex write.
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(Stamp.at(minutes))) + ".123Z"

    @staticmethod
    def ms(minutes: float) -> int:
        # Epoch milliseconds, like OpenCode writes.
        return int(Stamp.at(minutes) * 1000)


class Writer:
    """Writes fixture files with LF endings and sets their modification times."""

    @staticmethod
    def jsonl(path: Path, records: list, minutes: float, raw_lines: tuple = ()) -> Path:
        # Writes records as JSON lines (plus raw lines verbatim) and sets the mtime.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            for line in raw_lines:
                handle.write(line + "\n")
        os.utime(str(path), (Stamp.at(minutes), Stamp.at(minutes)))
        return path

    @staticmethod
    def json(path: Path, document: object, minutes: float = None, raw: str = None) -> Path:
        # Writes one JSON document (or raw text) and optionally sets the mtime.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(raw if raw is not None else json.dumps(document))
        if minutes is not None:
            os.utime(str(path), (Stamp.at(minutes), Stamp.at(minutes)))
        return path


class ClaudeFixtures:
    """Claude Code transcript records."""

    @staticmethod
    def user(text, minutes: float, cwd: str, **extra) -> dict:
        # A user record with string or list content.
        record = {"type": "user", "timestamp": Stamp.iso(minutes), "cwd": cwd, "sessionId": "x", "message": {"role": "user", "content": text}}
        record.update(extra)
        return record

    @staticmethod
    def assistant(minutes: float, cwd: str, text: str = "", tools: list = (), stop: str = "end_turn", model: str = "claude-opus-5-5") -> dict:
        # An assistant record with text and tool_use parts.
        parts = ([{"type": "text", "text": text}] if text else []) + list(tools)
        return {"type": "assistant", "timestamp": Stamp.iso(minutes), "cwd": cwd, "message": {"role": "assistant", "model": model, "content": parts, "stop_reason": None if tools else stop, "id": "m%s" % minutes}}

    @staticmethod
    def main_session(home: Path) -> list:
        # The main fixture: injected text, a slash command, an interrupt, a correction, a go-ahead, secrets, and todos.
        cwd = str(home / "repo")
        u, a = ClaudeFixtures.user, ClaudeFixtures.assistant
        return [
            u("fix this", 1, cwd),
            a(2, cwd, "Which file do you mean?"),
            u("<system-reminder>internal</system-reminder>", 3, cwd),
            u("Caveat: the messages below were generated by the user while running local commands.", 3.1, cwd),
            u("Base directory for this skill: C:/skills/x", 3.2, cwd),
            u("<command-name>/clear</command-name><command-args></command-args>", 3.3, cwd),
            u("<command-name>/design-doc</command-name><command-message>design-doc</command-message><command-args>build X</command-args>", 4, cwd),
            a(5, cwd, "", tools=[{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "src/app.py"}}, {"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {"todos": [{"content": "Write tests", "status": "pending"}, {"content": "Ship", "status": "completed"}]}}]),
            u([{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}], 6, cwd),
            u("[Request interrupted by user for tool use]", 7, cwd),
            u("no, I meant the other file", 8, cwd),
            {"type": "assistant", "isSidechain": True, "timestamp": Stamp.iso(8.5), "message": {"content": [{"type": "text", "text": "sidechain"}]}},
            a(9, cwd, "Done. Want me to push?"),
            u("yes", 10, cwd),
            a(11, cwd, "Pushed."),
            u("Use key %s and token %s with Authorization: Bearer abcdefghijklmnop1234 and %s; mail %s; my folder is %s" % (SECRET_KEY, SECRET_GH, SECRET_JWT, SECRET_EMAIL, str(home / "secret-project")), 12, cwd),
            a(13, cwd, "Noted."),
            {"type": "ai-title", "aiTitle": "Fix the app", "sessionId": "x"},
        ]

    @staticmethod
    def drip_session(cwd: str) -> list:
        # Drip-fed requirements plus emphasis, stakes, persona, and step-by-step prompts.
        u, a = ClaudeFixtures.user, ClaudeFixtures.assistant
        prompts = [
            "Build the export feature in src/export.py",
            "also add csv output",
            "oh and add json output",
            "btw add tests for both",
            "YOU MUST NEVER DELETE THIS FILE, it is shared",
            "Check the JSON API README TODO list for the format",
            "I'll give you a $200 tip if this works",
            "You are a world-class engineer, review the export code",
            "Think step by step about the edge cases",
            "Don't add logging. Never touch the config. Do not rename files.",
        ]
        records = []
        for index, text in enumerate(prompts):
            records.append(u(text, 100 + index * 2, cwd))
            records.append(a(101 + index * 2, cwd, "OK, done with step %d." % index))
        return records


class CodexFixtures:
    """Codex rollout records (top-level key order matches the real files)."""

    @staticmethod
    def rec(kind: str, payload: dict, minutes: float) -> dict:
        # One rollout line: timestamp, type, payload (payload's "type" first when it has one).
        return OrderedDict([("timestamp", Stamp.iso(minutes)), ("type", kind), ("payload", payload)])

    @staticmethod
    def interactive(cwd: str) -> list:
        # A CLI session with injected context, a doubled prompt, a tool call, a correction, and an abort.
        r = CodexFixtures.rec
        prompt = "Refactor src/db.py so the tests pass"
        return [
            r("session_meta", OrderedDict([("id", "codex-int-1"), ("cwd", cwd), ("originator", "codex_cli_rs"), ("source", "cli")]), 200),
            r("turn_context", OrderedDict([("cwd", cwd), ("model", "gpt-6-sol")]), 200.1),
            r("event_msg", OrderedDict([("type", "user_message"), ("message", "<environment_context>cwd</environment_context>")]), 200.2),
            r("event_msg", OrderedDict([("type", "user_message"), ("message", prompt)]), 201),
            r("event_msg", OrderedDict([("type", "item_completed"), ("item", {"id": "i1", "type": "UserMessage", "content": [{"type": "text", "text": prompt}]})]), 201.01),
            r("response_item", OrderedDict([("type", "function_call"), ("name", "shell_command"), ("arguments", json.dumps({"command": ["bash", "-lc", "pytest -q"]})), ("call_id", "c1")]), 202),
            r("response_item", OrderedDict([("type", "function_call_output"), ("call_id", "c1"), ("output", "Exit code: 1\nFAILED")]), 202.5),
            r("event_msg", OrderedDict([("type", "agent_message"), ("message", "Refactored; tests now pass."), ("phase", "final_answer")]), 203),
            r("event_msg", OrderedDict([("type", "task_complete"), ("last_agent_message", "Refactored; tests now pass.")]), 203.1),
            r("event_msg", OrderedDict([("type", "user_message"), ("message", "wrong, revert that change")]), 204),
            r("event_msg", OrderedDict([("type", "turn_aborted"), ("reason", "interrupted")]), 205),
        ]

    @staticmethod
    def skipped(session_id: str, cwd: str, originator: str, source, minutes: float) -> list:
        # A session that must be skipped (exec, subagent).
        r = CodexFixtures.rec
        return [
            r("session_meta", OrderedDict([("id", session_id), ("cwd", cwd), ("originator", originator), ("source", source)]), minutes),
            r("event_msg", OrderedDict([("type", "user_message"), ("message", "scripted prompt")]), minutes + 0.1),
        ]


class OpenCodeFixtures:
    """An OpenCode database with v2, v1, and child sessions, plus a legacy storage/ tree."""

    @staticmethod
    def build(data_dir: Path, cwd: str, with_v2: bool = True) -> Path:
        # Creates opencode.db (and storage/) under data_dir; returns the database path.
        data_dir.mkdir(parents=True, exist_ok=True)
        db = data_dir / "opencode.db"
        connection = sqlite3.connect(str(db))
        c = connection.cursor()
        c.execute("create table session (id text, project_id text, parent_id text, directory text, title text, time_created integer, time_updated integer, model text)")
        c.execute("create table message (id text, session_id text, time_created integer, time_updated integer, data text)")
        c.execute("create table part (id text, message_id text, session_id text, time_created integer, time_updated integer, data text)")
        c.execute("create table todo (session_id text, content text, status text, priority text, position integer, time_created integer, time_updated integer)")
        if with_v2:
            c.execute("create table session_v2 (id text, project_id text, parent_id text, directory text, title text, model text, time_created integer, time_updated integer)")
            c.execute("create table session_message (id text, session_id text, type text, seq integer, time_created integer, time_updated integer, data text)")
            model = json.dumps({"id": "gpt-6-luna", "providerID": "openai"})
            c.execute("insert into session_v2 values (?,?,?,?,?,?,?,?)", ("ses_v2a", "p", None, cwd, "Dark mode", model, Stamp.ms(300), Stamp.ms(306)))
            c.execute("insert into session_v2 values (?,?,?,?,?,?,?,?)", ("ses_child", "p", "ses_v2a", cwd, "Child", model, Stamp.ms(300), Stamp.ms(307)))
            c.execute("insert into session_v2 values (?,?,?,?,?,?,?,?)", ("ses_v1only", "p", None, cwd, "Flaky test", model, Stamp.ms(310), Stamp.ms(312)))
            rows = [
                ("user", 300, {"text": "Add a dark mode toggle to the settings page in src/settings.tsx"}),
                ("synthetic", 300.5, {"text": "synthetic context that is not a prompt"}),
                ("assistant", 301, {"model": {"id": "gpt-6-luna"}, "content": [{"type": "text", "text": "Added the toggle."}, {"type": "tool", "name": "shell", "state": {"status": "completed", "input": {"command": "npm test"}}}], "finish": "stop"}),
                ("user", 302, {"text": "stop, that's wrong"}),
                ("assistant", 303, {"model": {"id": "gpt-6-luna"}, "content": [], "finish": "error", "error": {"type": "aborted", "message": "Step interrupted"}}),
            ]
            for seq, (kind, minutes, data) in enumerate(rows):
                c.execute("insert into session_message values (?,?,?,?,?,?,?)", ("m%d" % seq, "ses_v2a", kind, seq, Stamp.ms(minutes), Stamp.ms(minutes), json.dumps(data)))
            c.execute("insert into session_message values (?,?,?,?,?,?,?)", ("mc", "ses_child", "user", 0, Stamp.ms(300), Stamp.ms(300), json.dumps({"text": "child prompt"})))
        c.execute("insert into session values (?,?,?,?,?,?,?,?)", ("ses_v1only", "p", None, cwd, "Flaky test", Stamp.ms(310), Stamp.ms(312), None))
        c.execute("insert into message values (?,?,?,?,?)", ("msg1", "ses_v1only", Stamp.ms(310), Stamp.ms(310), json.dumps({"role": "user", "time": {"created": Stamp.ms(310)}})))
        c.execute("insert into part values (?,?,?,?,?,?)", ("prt1", "msg1", "ses_v1only", Stamp.ms(310), Stamp.ms(310), json.dumps({"type": "text", "text": "Investigate the flaky login test in tests/test_login.py"})))
        c.execute("insert into part values (?,?,?,?,?,?)", ("prt2", "msg1", "ses_v1only", Stamp.ms(310.1), Stamp.ms(310.1), json.dumps({"type": "text", "text": "synthetic part", "synthetic": True})))
        c.execute("insert into message values (?,?,?,?,?)", ("msg2", "ses_v1only", Stamp.ms(311), Stamp.ms(311), json.dumps({"role": "assistant", "modelID": "gpt-6-luna"})))
        c.execute("insert into part values (?,?,?,?,?,?)", ("prt3", "msg2", "ses_v1only", Stamp.ms(311), Stamp.ms(311), json.dumps({"type": "text", "text": "It is a race in the fixture."})))
        c.execute("insert into part values (?,?,?,?,?,?)", ("prt4", "msg2", "ses_v1only", Stamp.ms(311.1), Stamp.ms(311.1), json.dumps({"type": "step-finish", "reason": "stop"})))
        c.execute("insert into todo values (?,?,?,?,?,?,?)", ("ses_v1only", "Fix the race", "pending", "high", 0, Stamp.ms(311), Stamp.ms(311)))
        connection.commit()
        connection.close()
        OpenCodeFixtures.legacy(data_dir, cwd)
        return db

    @staticmethod
    def legacy(data_dir: Path, cwd: str) -> None:
        # A legacy-only session, and a legacy copy of ses_v2a that must not be read twice.
        storage = data_dir / "storage"
        for session_id, minutes in (("ses_legacy", 320), ("ses_v2a", 300)):
            Writer.json(storage / "session" / "proj" / ("%s.json" % session_id), {"id": session_id, "directory": cwd, "title": "Legacy", "time": {"created": Stamp.ms(minutes), "updated": Stamp.ms(minutes + 1)}})
            Writer.json(storage / "message" / session_id / "msg_l1.json", {"id": "msg_l1_%s" % session_id, "role": "user", "time": {"created": Stamp.ms(minutes)}})
            Writer.json(storage / "part" / ("msg_l1_%s" % session_id) / "prt_1.json", {"type": "text", "text": "legacy prompt for %s about src/legacy.py" % session_id})


class GeminiFixtures:
    """Gemini CLI and Qwen Code chat files."""

    @staticmethod
    def chat(root: Path, project_dir_name: str, session_id: str, minutes: float, messages: list) -> Path:
        # Writes tmp/<project>/chats/session-*.json with the given messages.
        path = root / "tmp" / project_dir_name / "chats" / ("session-%s.json" % session_id[:8])
        return Writer.json(path, {"sessionId": session_id, "projectHash": project_dir_name, "startTime": Stamp.iso(minutes), "lastUpdated": Stamp.iso(minutes + 5), "messages": messages}, minutes + 5)

    @staticmethod
    def gemini_messages(minutes: float) -> list:
        # A list-of-parts prompt, a tool call, a /help, a retry, and a cancellation.
        return [
            {"type": "user", "timestamp": Stamp.iso(minutes), "content": [{"text": "Explain the build failure in ci.yml"}]},
            {"type": "gemini", "timestamp": Stamp.iso(minutes + 1), "model": "gemini-3-pro-preview", "content": "It fails because of a missing secret.", "toolCalls": [{"name": "read_file", "args": {"absolute_path": "ci.yml"}, "status": "success"}]},
            {"type": "user", "timestamp": Stamp.iso(minutes + 2), "content": "/help"},
            {"type": "user", "timestamp": Stamp.iso(minutes + 3), "content": "try again"},
            {"type": "info", "timestamp": Stamp.iso(minutes + 4), "content": "Request cancelled."},
        ]


class MessageLogFixtures:
    """Factory Droid and Pi message logs."""

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
        # A reminder-only message, a prompt with a reminder part, a UI notice, and a reply with a tool call.
        m, t = MessageLogFixtures.msg, MessageLogFixtures.text
        return [
            {"type": "session_start", "id": "fac-1", "title": "New Session", "sessionTitle": "Fix login", "owner": "me", "version": 2, "cwd": cwd},
            m(20, {"role": "user", "visibility": "llm_only", "content": [t("<system-reminder>tools</system-reminder>")]}),
            m(20.1, {"role": "user", "content": [t("<system-reminder>x</system-reminder>"), t("Fix the login redirect in app/auth.ts")]}),
            m(20.2, {"role": "user", "visibility": "user_only", "content": [t("No active subscription")]}),
            m(21, {"role": "assistant", "content": [t("Fixing."), {"type": "tool_use", "id": "t", "name": "Edit", "input": {"file_path": "app/auth.ts"}}]}),
            m(21.5, {"role": "assistant", "content": [t("Fixed: the redirect now goes to /home.")]}),
        ]

    @staticmethod
    def pi(cwd: str) -> list:
        # A failed tool call, an aborted reply, a correction, and a finished reply.
        m, t = MessageLogFixtures.msg, MessageLogFixtures.text
        return [
            {"type": "session", "version": 3, "id": "pi-1", "timestamp": Stamp.iso(30), "cwd": cwd},
            {"type": "model_change", "id": "x", "parentId": None, "timestamp": Stamp.iso(30), "provider": "deepseek", "modelId": "deepseek-v4-pro"},
            m(30.1, {"role": "user", "content": [t("List the files in src/")], "timestamp": Stamp.ms(30.1)}),
            m(30.2, {"role": "assistant", "model": "deepseek-v4-pro", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls src"}}], "stopReason": "toolUse"}),
            m(30.3, {"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": True, "content": [t("no such dir")]}),
            m(30.4, {"role": "assistant", "model": "deepseek-v4-pro", "content": [], "stopReason": "aborted"}),
            m(31, {"role": "user", "content": [t("no, list app/ instead")]}),
            m(31.5, {"role": "assistant", "model": "deepseek-v4-pro", "content": [t("Here they are.")], "stopReason": "stop"}),
        ]


class Home:
    """A temporary home folder holding every harness store."""

    def __init__(self, base: Path) -> None:
        # Builds every fixture under base/home.
        self.path = base / "home"
        self.repo = str(self.path / "repo")
        self.build()

    def build(self) -> None:
        # Writes the Claude, Codex, OpenCode, Gemini, Qwen, and not-read fixtures.
        h = self.path
        Writer.jsonl(h / ".claude" / "projects" / "C--repo" / "claude-main.jsonl", ClaudeFixtures.main_session(h), 13, raw_lines=('{"type":"user", broken json',))
        Writer.jsonl(h / ".claude" / "projects" / "C--repo" / "claude-sdk.jsonl", [dict(ClaudeFixtures.user("sdk prompt", 400, self.repo), entrypoint="sdk-py")], 400)
        Writer.jsonl(h / ".claude-alt" / "projects" / "C--repo" / "claude-drip.jsonl", ClaudeFixtures.drip_session(self.repo), 120)
        Writer.jsonl(h / ".claude-work" / "projects" / "C--repo" / "claude-work.jsonl", [ClaudeFixtures.user("work prompt", 50, self.repo)], 50)
        codex = h / ".codex" / "sessions" / "2026" / "10" / "01"
        Writer.jsonl(codex / "rollout-int.jsonl", CodexFixtures.interactive(self.repo), 205)
        Writer.jsonl(codex / "rollout-exec.jsonl", CodexFixtures.skipped("codex-exec", self.repo, "codex_exec", "exec", 206), 206)
        Writer.jsonl(codex / "rollout-sub.jsonl", CodexFixtures.skipped("codex-sub", self.repo, "codex_cli_rs", {"subagent": {"other": "guardian"}}, 207), 207)
        OpenCodeFixtures.build(h / ".local" / "share" / "opencode", self.repo)
        gemini_hash = hashlib.sha256(self.repo.encode("utf-8")).hexdigest()
        Writer.json(h / ".gemini" / "projects.json", {"projects": {self.repo: "repo"}})
        GeminiFixtures.chat(h / ".gemini", gemini_hash, "gem-session-1", 330, GeminiFixtures.gemini_messages(330))
        Writer.json(h / ".gemini" / "tmp" / gemini_hash / "chats" / "session-broken.json", None, 331, raw='{"sessionId": "broken", "messages": [')
        GeminiFixtures.chat(h / ".qwen", "proj", "qwen-session-1", 340, [{"type": "user", "timestamp": Stamp.iso(340), "content": "Summarize src/main.py"}, {"type": "qwen", "timestamp": Stamp.iso(341), "model": "qwen3-coder", "content": "It boots the app."}])
        (h / ".cursor" / "chats").mkdir(parents=True, exist_ok=True)
        (h / ".grok" / "sessions").mkdir(parents=True, exist_ok=True)
        Writer.jsonl(h / ".openclaude" / "projects" / "C--repo" / "openclaude-1.jsonl", [ClaudeFixtures.user("Rename the helper in src/util.py", 40, self.repo), ClaudeFixtures.assistant(41, self.repo, "Renamed.")], 41)
        Writer.jsonl(h / ".factory" / "sessions" / "-C-repo" / "fac-1.jsonl", MessageLogFixtures.factory(self.repo), 21.5)
        Writer.json(h / ".factory" / "sessions" / "-C-repo" / "fac-1.settings.json", {"model": "x"}, 21)
        Writer.jsonl(h / ".pi" / "agent" / "sessions" / "--C--repo--" / "2026-10-01T00-00-00-000Z_pi-1.jsonl", MessageLogFixtures.pi(self.repo), 31.5)


class FakeCli:
    """A recording fake vidbyte-cli: a real launcher (an .exe on Windows) made with pip's distlib."""

    def __init__(self, base: Path) -> None:
        # Writes fakecli.py and builds the launcher in base/fakebin.
        self.module_dir = base / "fakecli"
        self.module_dir.mkdir(parents=True, exist_ok=True)
        Writer.json(self.module_dir / "fakecli.py", None, raw=FAKE_CLI_SOURCE)
        self.bin_dir = base / "fakebin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.log = base / "fakecli-log.jsonl"
        from pip._vendor.distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, str(self.bin_dir))
        maker.executable = sys.executable
        maker.variants = {""}
        made = maker.make("vidbyte-cli = fakecli:main")
        self.path = made[0]

    def calls(self) -> list:
        # Every recorded call: {"argv": [...], "cwd": "..."}.
        if not self.log.exists():
            return []
        with open(str(self.log), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class Runner:
    """Runs prompt_review.py in a subprocess confined to the fixture home."""

    def __init__(self, home: Home, fake: FakeCli, base: Path) -> None:
        # Builds the confined environment once.
        self.home = home
        self.fake = fake
        self.env = self._env(base)

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

    def _env(self, base: Path) -> dict:
        # HOME and every harness folder in the fixture home; PATH without any vidbyte-cli.
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        h = self.home.path
        env.update({
            "HOME": str(h), "USERPROFILE": str(h), "CLAUDE_CONFIG_DIR": str(h / ".claude"), "CODEX_HOME": str(h / ".codex"),
            "XDG_DATA_HOME": str(h / ".local" / "share"), "APPDATA": str(h / "AppData" / "Roaming"), "LOCALAPPDATA": str(h / "AppData" / "Local"),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(base / "vb-state"), "FAKE_CLI_LOG": str(self.fake.log), "PYTHONPATH": str(self.fake.module_dir),
            "PYTHONIOENCODING": "utf-8", "PATH": self._clean_path(),
        })
        return env

    @staticmethod
    def _clean_path() -> str:
        # The current PATH minus every folder that holds a vidbyte-cli.
        keep = []
        for folder in os.environ.get("PATH", "").split(os.pathsep):
            if folder and not any(os.path.exists(os.path.join(folder, n)) for n in ("vidbyte-cli", "vidbyte-cli.exe", "vidbyte-cli.cmd", "vidbyte-cli.bat")):
                keep.append(folder)
        return os.pathsep.join(keep)


class SessionBuilder:
    """Builds in-memory sessions for signal and digest unit tests."""

    @staticmethod
    def make(prompts: list, harness: str = "claude-code", sid: str = "s", asks: tuple = (), minutes: float = 0) -> Session:
        # One session from prompt texts; indexes in `asks` make the agent end the previous turn with a question.
        session = Session(harness, sid, "mem")
        session.add_cwd("/repo")
        for index, text in enumerate(prompts):
            if index:
                session.note_agent_text("Should I continue?" if index in asks else "Done with that.")
                session.note_turn_end("finished")
            session.add_turn(text, Clock.parse_ts(Stamp.at(minutes + index * 30)))
        session.note_agent_text("All done.")
        session.note_turn_end("finished")
        return session.finish()


class UnitTests:
    """Section 10 unit tests."""

    def __init__(self, home: Home, base: Path) -> None:
        # Keeps the fixture home and a scratch folder.
        self.home = home
        self.base = base
        self.stats = {}

    def read_claude(self, name: str):
        # Reads one Claude fixture file with a fresh stats counter.
        from collections import Counter
        self.stats = Counter()
        reader = ClaudeCodeReader([self.home.path / ".claude"])
        candidate = next(c for c in reader.candidates(None) if Path(c.ref).name == name)
        return reader.read(candidate, None, self.stats)

    def redactor_secrets(self) -> None:
        # Redacts keys, a JWT, a bearer token, an email, home paths in two spellings; keeps git@github.com.
        login = "alicecooper"
        r = Redactor(Path("C:/Users/%s" % login))
        text = "%s %s %s Authorization: Bearer abcdefghijklmnop1234 %s C:\\Users\\%s\\repo C--Users-%s-repo git@github.com:o/r" % (SECRET_KEY, SECRET_GH, SECRET_JWT, SECRET_EMAIL, login, login)
        out = r(text)
        for leaked in (SECRET_KEY, SECRET_GH, SECRET_JWT, "abcdefghijklmnop1234", SECRET_EMAIL, login):
            Check.true(leaked not in out, "leaked %s in %s" % (leaked, out))
        Check.true("git@github.com" in out, "git@github.com was redacted")

    def redactor_none(self) -> None:
        # None becomes "".
        Check.equal(Redactor(Path("/home/someone"))(None), "")

    def add_turn_dedupe(self) -> None:
        # The same text within 10 seconds is kept once; after 10 seconds it is kept twice.
        s = Session("codex", "x", "y")
        t0 = Clock.parse_ts(Stamp.at(0))
        s.add_turn("run it", t0)
        s.add_turn("run it", Clock.parse_ts(Stamp.at(0) + 5))
        Check.equal(s.prompt_count, 1, "within 10s")
        s.add_turn("run it", Clock.parse_ts(Stamp.at(0) + 30))
        Check.equal(s.prompt_count, 2, "after 10s")

    def add_turn_blank(self) -> None:
        # A whitespace-only prompt is ignored.
        s = Session("codex", "x", "y")
        Check.equal(s.add_turn("   \n ", None), None)
        Check.equal(s.prompt_count, 0)

    def claude_injected(self) -> None:
        # Reminders, caveats, skill bodies, and tool results are not prompts; /design-doc build X is; /clear is not.
        texts = [t.text for t in self.read_claude("claude-main.jsonl").turns]
        Check.equal(texts[:4], ["fix this", "/design-doc build X", "no, I meant the other file", "yes"], "prompts")
        Check.true(not any("system-reminder" in t or "Caveat" in t or "Base directory" in t or t.startswith("/clear") for t in texts), "injected text kept")

    def claude_interrupt(self) -> None:
        # [Request interrupted by user] marks the /design-doc turn interrupted; the tool error counts as failed.
        session = self.read_claude("claude-main.jsonl")
        turn = session.turns[1]
        Check.true(turn.interrupted, "interrupt not marked")
        Check.equal(turn.failed_actions, 1, "failed actions")
        Check.equal(turn.actions[0], "Edit src/app.py", "action text")
        Check.equal(session.todos, [("Write tests", "pending"), ("Ship", "completed")], "todos")
        Check.equal(session.title, "Fix the app", "title")

    def claude_sdk(self) -> None:
        # entrypoint sdk-py gives skip reason sdk.
        Check.equal(self.read_claude("claude-sdk.jsonl").skip_reason, "sdk")

    def claude_corrupt_line(self) -> None:
        # A corrupt line is counted and parsing continues.
        session = self.read_claude("claude-main.jsonl")
        Check.equal(self.stats["unreadable_lines"], 1, "unreadable lines")
        Check.true(session.prompt_count >= 5, "parsing stopped early")

    def codex_skips(self) -> None:
        # An exec session is non_interactive and a guardian thread is a subagent.
        from collections import Counter
        reader = CodexReader(self.home.path / ".codex")
        reasons = {Path(c.ref).name: reader.read(c, None, Counter()).skip_reason for c in reader.candidates(None)}
        Check.equal(reasons["rollout-exec.jsonl"], "non_interactive")
        Check.equal(reasons["rollout-sub.jsonl"], "subagent")
        Check.equal(reasons["rollout-int.jsonl"], "")

    def codex_reading(self) -> None:
        # Environment context is not a prompt, the doubled prompt is kept once, the tool call counts, and the abort interrupts.
        from collections import Counter
        reader = CodexReader(self.home.path / ".codex")
        candidate = next(c for c in reader.candidates(None) if Path(c.ref).name == "rollout-int.jsonl")
        session = reader.read(candidate, None, Counter())
        Check.equal([t.text for t in session.turns], ["Refactor src/db.py so the tests pass", "wrong, revert that change"])
        Check.equal(session.turns[0].tool_calls, 1, "tool calls")
        Check.equal(session.turns[0].failed_actions, 1, "failed exit code")
        Check.true(session.turns[1].interrupted, "abort not marked")
        Check.equal(session.end_state, "interrupted")
        Check.equal(dict(session.models), {"gpt-6-sol": 1})

    def opencode_reader(self, data_dir: Path = None) -> OpenCodeReader:
        # An OpenCode reader on the fixture data folder.
        return OpenCodeReader(data_dir or self.home.path / ".local" / "share" / "opencode")

    def opencode_v2(self) -> None:
        # v2 rows map to turns, synthetic rows are ignored, the abort interrupts, and child sessions are not candidates.
        from collections import Counter
        reader = self.opencode_reader()
        candidates = {c.ref: c for c in reader.candidates(None)}
        Check.true("ses_child" not in candidates, "child session listed")
        session = reader.read(candidates["ses_v2a"], None, Counter())
        Check.equal([t.text for t in session.turns], ["Add a dark mode toggle to the settings page in src/settings.tsx", "stop, that's wrong"])
        Check.true(session.turns[1].interrupted, "abort not marked")
        Check.equal(session.turns[0].actions, ["shell: npm test"])
        reader.close()

    def opencode_v1(self) -> None:
        # A session with no v2 messages is read from v1; a synthetic part is not a prompt; todos come from the table.
        from collections import Counter
        reader = self.opencode_reader()
        candidates = {c.ref: c for c in reader.candidates(None)}
        Check.equal(candidates["ses_v1only"].extra, "v1", "backend")
        session = reader.read(candidates["ses_v1only"], None, Counter())
        Check.equal([t.text for t in session.turns], ["Investigate the flaky login test in tests/test_login.py"])
        Check.equal(session.end_state, "finished")
        Check.equal(session.todos, [("Fix the race", "pending")])
        reader.close()

    def opencode_legacy(self) -> None:
        # A legacy-only session is read; ses_v2a, also in storage/, is listed once (from the database).
        from collections import Counter
        reader = self.opencode_reader()
        listed = [c.ref for c in reader.candidates(None)]
        Check.equal(listed.count("ses_v2a"), 1, "ses_v2a listed")
        legacy = next(c for c in reader.candidates(None) if c.ref == "ses_legacy")
        session = reader.read(legacy, None, Counter())
        Check.equal([t.text for t in session.turns], ["legacy prompt for ses_legacy about src/legacy.py"])
        reader.close()

    def opencode_no_v2(self) -> None:
        # An older database without session_v2 falls back to v1.
        from collections import Counter
        data_dir = self.base / "oc-old"
        OpenCodeFixtures.build(data_dir, "/repo", with_v2=False)
        reader = self.opencode_reader(data_dir)
        candidate = next(c for c in reader.candidates(None) if c.ref == "ses_v1only")
        Check.equal(candidate.extra, "v1")
        Check.equal(reader.read(candidate, None, Counter()).prompt_count, 1)
        reader.close()

    def opencode_bad_db(self) -> None:
        # A non-SQLite opencode.db is reported in read[] and the other harnesses still run.
        data_dir = self.base / "oc-bad"
        Writer.json(data_dir / "opencode.db", None, raw="this is not a database" * 50)
        options = CollectOptions(opencode_dir=str(data_dir), codex_home=str(self.home.path / ".codex"), disabled={"claude", "gemini", "qwen"})
        result = SessionCollector(options, home=self.home.path).collect()
        entry = next(e for e in result.read if e["harness"] == "opencode")
        Check.equal(entry["status"], "error", "status")
        Check.true("opencode.db not read" in entry.get("error", ""), "read[] has no error for a bad opencode.db: %r" % entry)
        summary_entry = next(e for e in result.summary(Redactor(self.home.path))["read"] if e["harness"] == "opencode")
        Check.true(str(self.base) not in json.dumps(summary_entry), "the summary leaks the home path: %r" % summary_entry)
        Check.true(any(s.harness == "codex" for s in result.sessions), "codex not read after the OpenCode failure")

    def gemini_reading(self) -> None:
        # List-of-parts content is flattened, /help is dropped, cancellation interrupts, the folder resolves by hash, and Qwen is qwen-code.
        from collections import Counter
        reader = GeminiReader(self.home.path / ".gemini")
        session = reader.read(next(c for c in reader.candidates(None) if "gem-sess" in Path(c.ref).name), None, Counter())
        Check.equal([t.text for t in session.turns], ["Explain the build failure in ci.yml", "try again"])
        Check.true(session.turns[1].interrupted, "cancellation not marked")
        Check.equal(session.cwd, self.home.repo, "project folder")
        Check.equal(session.turns[0].actions, ["Read ci.yml"])
        qwen = GeminiReader(self.home.path / ".qwen", "qwen-code")
        qsession = qwen.read(qwen.candidates(None)[0], None, Counter())
        Check.equal(qsession.harness, "qwen-code")

    def gemini_truncated(self) -> None:
        # A truncated chat file is counted as unreadable and skipped.
        from collections import Counter
        stats = Counter()
        reader = GeminiReader(self.home.path / ".gemini")
        broken = next(c for c in reader.candidates(None) if "broken" in Path(c.ref).name)
        Check.equal(reader.read(broken, None, stats), None)
        Check.equal(stats["unreadable_files"], 1)

    def openclaude_reading(self) -> None:
        # ~/.openclaude is read by default in Claude Code's format and labeled openclaude.
        options = CollectOptions(disabled={"claude", "codex", "opencode", "gemini", "qwen", "factory", "pi"})
        result = SessionCollector(options, home=self.home.path).collect()
        Check.equal([(s.harness, s.turns[0].text) for s in result.sessions], [("openclaude", "Rename the helper in src/util.py")])

    def factory_reading(self) -> None:
        # Reminders and UI notices are not prompts, the default title is dropped, and the reply's tool call counts.
        from collections import Counter
        reader = FactoryReader(self.home.path / ".factory")
        candidates = reader.candidates(None)
        Check.equal([Path(c.ref).name for c in candidates], ["fac-1.jsonl"], "candidates (settings files are not sessions)")
        session = reader.read(candidates[0], None, Counter())
        Check.equal([t.text for t in session.turns], ["Fix the login redirect in app/auth.ts"])
        Check.equal((session.session_id, session.title, session.cwd, session.harness), ("fac-1", "Fix login", self.home.repo, "factory-droid"))
        Check.equal(session.turns[0].actions, ["Edit app/auth.ts"])
        Check.equal(session.end_state, "finished")

    def pi_reading(self) -> None:
        # A failed tool result counts, an aborted reply interrupts, thinking is not agent text, and the reply model counts.
        from collections import Counter
        reader = PiReader(self.home.path / ".pi" / "agent")
        session = reader.read(reader.candidates(None)[0], None, Counter())
        Check.equal([t.text for t in session.turns], ["List the files in src/", "no, list app/ instead"])
        first = session.turns[0]
        Check.equal(first.actions, [ActionText.describe("bash", {"command": "ls src"})])
        Check.equal((first.failed_actions, first.interrupted), (1, True))
        Check.true("hmm" not in (first.reply_text or ""), "thinking leaked into the reply")
        Check.equal((session.session_id, session.end_state, dict(session.models)), ("pi-1", "finished", {"deepseek-v4-pro": 3}))
        expanded = '<skill name="design-doc" location="C:/x/SKILL.md">\nBODY\n</skill>\n\nbuild the export page'
        Check.equal(reader.clean_prompt(expanded), "/design-doc build the export page", "skill expansion")
        Check.equal(reader.clean_prompt("a <skill> tag mid-prompt"), "a <skill> tag mid-prompt", "plain prompt changed")

    def collector_only(self) -> None:
        # --only keeps the named harnesses and overrides --no-*; an unknown name exits 2.
        parser = argparse.ArgumentParser()
        CollectOptions.add_arguments(parser)
        options = CollectOptions.from_args(parser.parse_args(["--only", "codex", "--only", "pi", "--no-pi"]))
        everything = {"claude", "codex", "opencode", "gemini", "qwen", "openclaude", "factory", "pi"}
        Check.equal(sorted(everything - options.disabled), ["codex", "pi"])
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["--only", "cursor"])
        except SystemExit as stop:
            Check.equal(stop.code, 2)
            return
        raise AssertionError("--only cursor was accepted")

    def collector_cap(self) -> None:
        # With more sessions than the cap, keeps the newest across harnesses, newest first; skipped sessions do not count.
        options = CollectOptions(max_sessions=5, claude_dirs=[str(self.home.path / ".claude-alt")], codex_home=str(self.home.path / ".codex"), opencode_dir=str(self.home.path / ".local" / "share" / "opencode"), gemini_dir=str(self.home.path / ".gemini"), qwen_dir=str(self.home.path / ".qwen"))
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home.path / ".claude")
        try:
            result = SessionCollector(options, home=self.home.path).collect()
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        Check.equal(len(result.sessions), 5, "kept")
        stamps = [s.last_ts for s in result.sessions]
        Check.equal(stamps, sorted(stamps, reverse=True), "order")
        Check.equal([s.session_id for s in result.sessions], ["qwen-session-1", "gem-session-1", "ses_legacy", "ses_v1only", "ses_v2a"])
        Check.true(result.skipped["sdk"] == 1, "the newer sdk session should be skipped without using a slot: %r" % dict(result.skipped))

    def collector_bounds(self) -> None:
        # --max-sessions 0 and 501 are usage errors (exit 2).
        for value in ("0", "501"):
            parser = argparse.ArgumentParser()
            CollectOptions.add_arguments(parser)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(["--max-sessions", value])
            except SystemExit as stop:
                Check.equal(stop.code, 2, "exit for %s" % value)
                continue
            raise AssertionError("--max-sessions %s was accepted" % value)

    def collector_project_dir(self) -> None:
        # --project-dir keeps the folder and below, and drops a sibling with a shared prefix.
        root = self.base / "pd"
        for name, cwd in (("a.jsonl", str(root / "repo")), ("b.jsonl", str(root / "repo" / "sub")), ("c.jsonl", str(root / "repo2"))):
            Writer.jsonl(root / ".claude" / "projects" / "p" / name, [ClaudeFixtures.user("work on %s" % name, 10, cwd)], 10)
        options = CollectOptions(project_dirs=[str(root / "repo")], claude_dirs=[str(root / ".claude")], disabled={"codex", "opencode", "gemini", "qwen"})
        result = SessionCollector(options, home=root).collect()
        Check.equal(sorted(s.session_id for s in result.sessions), ["a", "b"])
        Check.equal(result.skipped["outside_project"], 1)

    def collector_empty(self) -> None:
        # No harness folders: 0 sessions, no crash.
        empty = self.base / "empty-home"
        empty.mkdir(exist_ok=True)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        result = SessionCollector(CollectOptions(), home=empty).collect()
        Check.equal(len(result.sessions), 0)

    def signals_correction(self) -> None:
        # "no, I meant the other file" is a correction; a pasted log with "no," deep inside is not; "stop the runner" is not.
        good = SessionBuilder.make(["Update src/a.py", "no, I meant the other file"])
        log = SessionBuilder.make(["Update src/a.py", "Here is the log: " + "x " * 80 + "no, it failed again"])
        instruction = SessionBuilder.make(["Run the job", "stop the runner and merge"])
        ids = lambda s: {sig.id for sig in SignalDetector().detect([s])}
        Check.true("correction" in ids(good), "correction missed")
        Check.true("correction" not in ids(log), "pasted log counted")
        Check.true("correction" not in ids(instruction), "instruction counted")

    def signals_go_ahead(self) -> None:
        # "yes" after a question is a go-ahead; "yes" after a plain statement is not.
        asked = SessionBuilder.make(["Plan the migration in db/", "yes"], asks=(1,))
        plain = SessionBuilder.make(["Plan the migration in db/", "yes"])
        Check.true("go_ahead" in {s.id for s in SignalDetector().detect([asked])}, "go-ahead missed")
        Check.true("go_ahead" not in {s.id for s in SignalDetector().detect([plain])}, "go-ahead without a question")

    def signals_vague_opener(self) -> None:
        # "fix this" as an opener is vague; a 5-word opener naming src/app.py is not.
        vague = SessionBuilder.make(["fix this"])
        named = SessionBuilder.make(["fix the bug in src/app.py"])
        Check.true("vague_opener" in {s.id for s in SignalDetector().detect([vague])}, "vague opener missed")
        Check.true("vague_opener" not in {s.id for s in SignalDetector().detect([named])}, "named opener flagged")

    def signals_drip(self) -> None:
        # Three "also" follow-ups make one drip_feed hit; two do not.
        three = SessionBuilder.make(["Build the export in src/x.py", "also csv", "also json", "also xml"])
        two = SessionBuilder.make(["Build the export in src/x.py", "also csv", "also json"])
        hits = [s for s in SignalDetector().detect([three]) if s.id == "drip_feed"]
        Check.equal(len(hits[0].hits) if hits else 0, 1, "drip hits")
        Check.true(not [s for s in SignalDetector().detect([two]) if s.id == "drip_feed"], "two follow-ups flagged")

    def signals_emphasis(self) -> None:
        # Acronyms are not shouting; "YOU MUST NEVER DELETE THIS" is; placeholders in angle brackets are not.
        acronyms = SessionBuilder.make(["Check the JSON API README TODO list"])
        shout = SessionBuilder.make(["YOU MUST NEVER DELETE THIS"])
        placeholder = SessionBuilder.make(["Fill in <INSERT PR NUMBER HERE> and <YOUR REPO NAME HERE>"])
        has = lambda s: "emphasis" in {x.id for x in SignalDetector().detect([s])}
        Check.true(not has(acronyms), "acronyms counted")
        Check.true(has(shout), "shouting missed")
        Check.true(not has(placeholder), "placeholders counted")

    def signals_tricks(self) -> None:
        # A $200 tip, a world-class persona, and "think step by step" are each detected.
        s = SessionBuilder.make(["I'll give you a $200 tip", "You are a world-class engineer, review it", "Think step by step"])
        ids = {x.id for x in SignalDetector().detect([s])}
        for wanted in ("emotional_stakes", "persona", "step_by_step"):
            Check.true(wanted in ids, "%s missed" % wanted)

    def signals_empty(self) -> None:
        # A session with no prompts gives no hits and no division by zero.
        empty = Session("claude-code", "e", "mem").finish()
        detector = SignalDetector()
        Check.equal(detector.detect([empty]), [])
        overall = detector.overall([empty], [])
        Check.equal(overall["prompts"], 0)
        Check.equal(detector.overall([], [])["openers_short_share"], 0.0)

    def signals_ranking(self) -> None:
        # Signals rank by distinct sessions, then hits.
        sessions = [SessionBuilder.make(["Update src/a.py", "yes"], sid="a%d" % i, asks=(1,)) for i in range(3)]
        sessions.append(SessionBuilder.make(["Update src/a.py", "no, wrong file", "no, still wrong", "no, not that"], sid="c"))
        ranked = [s.id for s in SignalDetector().detect(sessions)]
        Check.equal(ranked[:2], ["go_ahead", "correction"])

    def signals_templates(self) -> None:
        # A long template reused 3 times collapses to one persona hit that carries its uses.
        template = "# Review skill\n\nYou are a senior engineer. " + ("Check every file carefully. " * 60)
        sessions = [SessionBuilder.make([template], sid="t%d" % i, minutes=i) for i in range(3)]
        signal = next(s for s in SignalDetector().detect(sessions, TemplateIndex(sessions)) if s.id == "persona")
        Check.equal(len(signal.hits), 1, "collapsed hits")
        Check.equal(signal.prompt_count, 3, "uses counted")
        Check.equal(signal.template_count, 1, "templates")

    def digest_budget(self) -> None:
        # The digest never exceeds max_chars, and every shown signal has at least one example.
        sessions = [SessionBuilder.make(["fix this", "no, wrong %d" % i, "also add tests", "also docs", "also lint", "YOU MUST NEVER STOP " + "word " * 400], sid="b%d" % i, minutes=i) for i in range(120)]
        out = self.base / "digest-budget"
        detector = SignalDetector()
        signals = detector.detect(sessions)
        writer = DigestWriter(out, Redactor(Path.home()), 20_000, SKILL / "references" / "prompting-rubric.md")
        summary = writer.write(sessions, signals, detector.overall(sessions, signals), {"first_day": "a", "last_day": "b", "max_sessions": 500}, {})
        text = (out / "digest.md").read_text(encoding="utf-8")
        Check.true(len(text) <= 20_000, "digest is %d characters" % len(text))
        Check.equal(summary["digest_chars"], len(text))
        sections = text.split("### [sig-")[1:]
        Check.true(sections, "no signal sections")
        for section in sections:
            Check.true("\n- [" in section, "a signal section has no example: %s" % section[:60])

    def rubric_copy(self) -> None:
        # The rubric is under 20,000 bytes and copied byte for byte.
        out = self.base / "digest-budget"
        source = SKILL / "references" / "prompting-rubric.md"
        Check.true(source.stat().st_size <= 20_000, "rubric is %d bytes" % source.stat().st_size)
        Check.equal((out / "prompting-rubric.md").read_bytes(), source.read_bytes(), "rubric copy")

    def validator_problems(self) -> None:
        # An unknown key, a 9th mistake, a long value, an untagged mistake, and an unknown tag each give a problem.
        validator = PromptingRun.validator({"P1", "S1"}, Redactor(Path.home()))
        data = {"goal": "g", "bogus": 1, "mistakes": ["[P1] ok"] * 8 + ["no tag here"], "outcomes": ["[P1] " + "x" * 600, "[P77] unknown"]}
        _inputs, problems = validator.validate(data)
        joined = "\n".join(problems)
        for needle in ("Unknown key 'bogus'", "mistakes has 9 values", "characters; keep each value under 500", "must start with a bracketed digest tag", "names P77"):
            Check.true(needle in joined, "missing problem %r in %s" % (needle, joined))

    def validator_untagged_kinds(self) -> None:
        # avoid and constraint values are accepted without tags.
        validator = PromptingRun.validator({"P1"}, Redactor(Path.home()))
        _inputs, problems = validator.validate({"goal": "g", "mistakes": ["[P1] x"], "avoid": ["no longer openers advice"], "constraint": ["I dictate by voice"]})
        Check.equal(problems, [])

    def validator_redacts(self) -> None:
        # A value with an sk- key is redacted before it reaches argv.
        validator = PromptingRun.validator({"P1"}, Redactor(Path.home()))
        inputs, problems = validator.validate({"goal": "g", "mistakes": ["[P1] leaked %s here" % SECRET_KEY]})
        Check.equal(problems, [])
        argv = PromptingRun.argv("exe", inputs, False)
        Check.true(SECRET_KEY not in " ".join(argv), "secret reached argv")

    def argv_shape(self) -> None:
        # Root options first, six categories, count, horizon, files, instructions, token cap; --dry-run and --project only when asked.
        inputs = {"goal": "g", "project": None, "values": OrderedDict([("mistakes", ["[P1] m"])])}
        argv = PromptingRun.argv("exe", inputs, False)
        Check.equal(argv[:7], ["exe", "--json", "--no-input", "agents", "suggest", "run", "--goal"])
        Check.equal([argv[i + 1] for i, a in enumerate(argv) if a == "--category"], list(CATEGORIES))
        for pair in (("--count", "8"), ("--horizon", "any"), ("--files", "digest.md"), ("--instructions", "prompting-rubric.md"), ("--max-total-tokens", "400000")):
            Check.equal(argv[argv.index(pair[0]) + 1], pair[1], pair[0])
        Check.true("--dry-run" not in argv and "--project" not in argv, "dry-run or project without asking")
        inputs["project"] = "improve-my-prompting"
        dry = PromptingRun.argv("exe", inputs, True)
        Check.equal(dry[-1], "--dry-run")
        Check.equal(dry[dry.index("--project") + 1], "improve-my-prompting")

    def cost_estimate(self) -> None:
        # gpt-6-sol rates x1.02; None for another model or zero calls.
        usage = {"model_calls": 5, "input_tokens": 300000, "cached_input_tokens": 100000, "output_tokens": 50000}
        Check.equal(CostEstimator.estimate(usage, ["gpt-6-sol"]), 0.94)
        Check.equal(CostEstimator.estimate(usage, ["gpt-6-sol", "other"]), None)
        Check.equal(CostEstimator.estimate(dict(usage, model_calls=0), ["gpt-6-sol"]), None)
        report = RunReporter.summarize(7, self.base / "missing.json", self.base / "missing.txt", {"code": "x", "message": "m", "other": 1})
        Check.equal(report["error"], {"code": "x", "message": "m"})

    def rubric_index(self) -> None:
        # Every rubric ID in the shipped rubric parses (39 items), and every signal's rubric IDs exist.
        index = RubricIndex((SKILL / "references" / "prompting-rubric.md").read_text(encoding="utf-8"))
        Check.equal(len(index.items), 39, "rubric items")
        for definition in SignalCatalog.default():
            for rubric_id in definition.rubric:
                Check.true(index.lookup(rubric_id), "%s (%s) is not in the rubric" % (rubric_id, definition.id))
        Check.true(index.lookup("RT1")["source"].startswith("Anthropic"), "RT1 source")
        Check.true("feeds Y" in index.lookup("RT1")["text"], "wrapped item text was cut")

    def idea_reporter(self) -> None:
        # Resolves [P2] and RC3, flags an untagged idea as ungrounded, and lists unknown tags.
        out = self.base / "report-unit"
        out.mkdir(exist_ok=True)
        shutil.copyfile(str(SKILL / "references" / "prompting-rubric.md"), str(out / "prompting-rubric.md"))
        Writer.json(out / "sessions.json", {"sessions": [{"tag": "S1", "harness": "codex", "project": "repo"}], "prompts": [{"tag": "P2", "session": "S1", "text": "fix this", "signals": ["vague_opener"]}]})
        ideas = [{"id": "idea-001", "rank": 1, "title": "A", "summary": "See [P2] for RC3."}, {"id": "idea-002", "rank": 2, "title": "B", "summary": "General."}, {"id": "idea-003", "rank": 3, "title": "C", "summary": "[P99] and RZ1 RX9"}]
        Writer.json(out / "result.json", {"schema_version": 1, "kind": "suggestions.result", "data": {"status": "complete", "ideas": ideas}})
        report = IdeaReporter(out).build()
        first, second, third = report["ideas"]
        Check.equal([p["tag"] for p in first["prompts"]], ["P2"])
        Check.equal([r["id"] for r in first["rubric"]], ["RC3"])
        Check.equal(first["signals"], ["vague_opener"])
        Check.equal((first["grounded"], second["grounded"], third["grounded"]), (True, False, False))
        Check.equal(third["unknown_tags"], ["P99"])
        Check.equal(third["unknown_rubric_ids"], ["RX9"])
        Check.equal(report["ungrounded"], ["idea-002", "idea-003"])


class IntegrationTests:
    """Section 10 integration tests through prompt_review.py subprocesses."""

    def __init__(self, home: Home, fake: FakeCli, runner: Runner, base: Path) -> None:
        # Keeps the fixtures, the fake CLI, and the runner.
        self.home = home
        self.fake = fake
        self.runner = runner
        self.base = base
        self.out = base / "out-main"

    def gather_end_to_end(self) -> None:
        # All five harnesses are read (Claude from two folders); counts, folders not read, and tools not read are reported.
        code, doc, out, err = self.runner.run("gather", "--claude-dir", str(self.home.path / ".claude-alt"), "--out", str(self.out))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-400:])
        read = doc["read"]
        Check.equal(read["kept_by_harness"], {"qwen-code": 1, "gemini-cli": 1, "opencode": 3, "codex": 1, "claude-code": 2, "openclaude": 1, "factory-droid": 1, "pi": 1}, "kept_by_harness")
        Check.equal(read["skipped"].get("sdk"), 1, "sdk skipped")
        Check.equal(read["skipped"].get("non_interactive"), 1, "exec skipped")
        Check.equal(read["skipped"].get("subagent"), 1, "subagent skipped")
        Check.true(any(p.endswith(".claude-work") for p in read["other_claude_dirs_not_read"]), "claude-work not listed: %r" % read["other_claude_dirs_not_read"])
        Check.true("Cursor" in read["not_read"] and "Grok CLI" in read["not_read"], "Cursor or Grok CLI not listed: %r" % read["not_read"])
        Check.equal(read["unreadable"].get("unreadable_lines"), 1, "corrupt line")
        Check.equal([e for e in read["read"] if e["status"] == "error"], [], "read[] errors")
        for name in ("digest.md", "sessions.json", "inputs.json", "prompting-rubric.md"):
            Check.true((self.out / name).is_file(), "%s missing" % name)

    def gather_no_secrets(self) -> None:
        # No raw secret, email, or home path reaches digest.md, inputs.json, or sessions.json.
        for name in ("digest.md", "inputs.json", "sessions.json"):
            text = (self.out / name).read_text(encoding="utf-8")
            for leaked in (SECRET_KEY, SECRET_GH, SECRET_JWT, SECRET_EMAIL, "abcdefghijklmnop1234", str(self.home.path), str(self.home.path).replace("\\", "/")):
                Check.true(leaked not in text, "%s leaked into %s" % (leaked[:24], name))

    def gather_tags(self) -> None:
        # Every tag in digest.md and inputs.json exists in sessions.json, and index tags are unique.
        import re
        index = json.loads((self.out / "sessions.json").read_text(encoding="utf-8"))
        known = [e["tag"] for e in index["sessions"]] + [e["tag"] for e in index["prompts"]]
        Check.equal(len(known), len(set(known)), "duplicate tags")
        for name in ("digest.md", "inputs.json"):
            used = set(re.findall(r"\b([PS]\d+)\b", " ".join(re.findall(r"\[([PS0-9 ,]+)\]", (self.out / name).read_text(encoding="utf-8")))))
            Check.true(used and used <= set(known), "%s uses tags not in sessions.json: %s" % (name, sorted(used - set(known))))

    def gather_digest_small(self) -> None:
        # --max-chars below 20,000 is a usage error.
        code, _doc, _out, _err = self.runner.run("gather", "--max-chars", "19999", "--out", str(self.base / "out-small"))
        Check.equal(code, 2)

    def gather_one_session(self) -> None:
        # --max-sessions 1 keeps exactly one session.
        code, doc, _out, _err = self.runner.run("gather", "--max-sessions", "1", "--out", str(self.base / "out-one"))
        Check.equal(code, 0)
        Check.equal(doc["read"]["kept"], 1)

    def gather_nothing(self) -> None:
        # Every harness disabled exits 3 and names the folders read.
        code, doc, _out, _err = self.runner.run("gather", "--no-claude", "--no-codex", "--no-opencode", "--no-gemini", "--no-qwen", "--no-openclaude", "--no-factory", "--no-pi", "--out", str(self.base / "out-none"))
        Check.equal(code, 3)
        Check.true("error" in doc and "read" in doc, "summary lacks error or read")

    def current_session_excluded(self) -> None:
        # The running session (CODEX_THREAD_ID) is left out unless --include-current.
        code, doc, _o, _e = self.runner.run("gather", "--out", str(self.base / "out-cur"), extra_env={"CODEX_THREAD_ID": "codex-int-1"})
        Check.equal(code, 0)
        Check.equal(doc["read"]["skipped"].get("current_session"), 1)
        code, doc, _o, _e = self.runner.run("gather", "--include-current", "--out", str(self.base / "out-cur2"), extra_env={"CODEX_THREAD_ID": "codex-int-1"})
        Check.equal(doc["read"]["skipped"].get("current_session"), None)

    def suggest_dry_run(self) -> None:
        # The fake records the expected argv, run from the output folder with relative files; no paid-run.txt.
        before = len(self.fake.calls())
        code, doc, _out, err = self.runner.run("suggest", "--out", str(self.out), "--dry-run", "--cli", self.fake.path)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-400:])
        Check.equal(doc["stop_reason"], "dry_run")
        call = self.fake.calls()[before]
        Check.equal(Path(call["cwd"]).resolve(), self.out.resolve(), "cwd")
        inputs = json.loads((self.out / "inputs.json").read_text(encoding="utf-8"))
        validator = PromptingRun.validator({e["tag"] for k in ("sessions", "prompts") for e in json.loads((self.out / "sessions.json").read_text(encoding="utf-8"))[k]}, Redactor(self.home.path))
        expected = PromptingRun.argv("X", validator.validate(inputs)[0], True)[1:]
        Check.equal(call["argv"], expected, "argv")
        Check.true(not (self.out / "paid-run.txt").exists(), "dry run wrote paid-run.txt")

    def suggest_paid_once(self) -> None:
        # --paid writes paid-run.txt then result.json; a second --paid exits 2 and the fake records no second call.
        code, doc, _out, err = self.runner.run("suggest", "--out", str(self.out), "--paid", "--cli", self.fake.path)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-400:])
        Check.true((self.out / "paid-run.txt").is_file() and (self.out / "result.json").is_file(), "marker or result missing")
        Check.equal(doc["estimated_cost_usd"], 0.94)
        calls = len(self.fake.calls())
        code, _doc, _out, _err = self.runner.run("suggest", "--out", str(self.out), "--paid", "--cli", self.fake.path)
        Check.equal(code, 2, "second paid run")
        Check.equal(len(self.fake.calls()), calls, "the fake was called again")

    def suggest_cli_error(self) -> None:
        # A CLI exit 5 with an error JSON on stderr is reported with error.code and exit 5.
        out = self.base / "out-err"
        shutil.copytree(str(self.out), str(out), ignore=shutil.ignore_patterns("paid-run.txt", "result*", "dry-run*", "report.md"))
        code, doc, _out, _err = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.fake.path, extra_env={"FAKE_CLI_EXIT": "5"})
        Check.equal(code, 5)
        Check.equal(doc["error"]["code"], "insufficient_credits")

    def suggest_no_cli(self) -> None:
        # No --cli and no vidbyte-cli on PATH exits 127 and runs nothing.
        out = self.base / "out-nocli"
        shutil.copytree(str(self.out), str(out), ignore=shutil.ignore_patterns("paid-run.txt", "result*", "dry-run*", "report.md"))
        calls = len(self.fake.calls())
        code, _doc, _out, _err = self.runner.run("suggest", "--out", str(out), "--dry-run")
        Check.equal(code, 127)
        Check.equal(len(self.fake.calls()), calls, "something ran")

    def report_after_paid(self) -> None:
        # report writes report.md quoting the redacted text of cited prompts and flags ungrounded ideas.
        code, doc, _out, err = self.runner.run("report", "--out", str(self.out))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        Check.equal(doc["ungrounded"], ["idea-002", "idea-003"])
        index = json.loads((self.out / "sessions.json").read_text(encoding="utf-8"))
        p1 = next(p for p in index["prompts"] if p["tag"] == "P1")
        text = (self.out / "report.md").read_text(encoding="utf-8")
        Check.true(p1["text"][:40] in text, "P1 text not quoted in report.md")
        Check.true("Not grounded" in text, "ungrounded warning missing")

    def feedback_accept(self) -> None:
        # feedback sends the exact title and the project from inputs.json; --reason is redacted.
        inputs_path = self.out / "inputs.json"
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        inputs["project"] = "improve-my-prompting"
        Writer.json(inputs_path, inputs)
        calls = len(self.fake.calls())
        code, _doc, _out, err = self.runner.run("feedback", "--out", str(self.out), "--idea", "idea-001", "--accept", "--reason", "great, my key %s" % SECRET_KEY, "--cli", self.fake.path)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        argv = self.fake.calls()[calls]["argv"]
        Check.equal(argv[:6], ["--json", "agents", "suggest", "feedback", "accept", "--project"])
        Check.equal(argv[argv.index("--suggestion") + 1], "State the done-check in your first message")
        Check.equal(argv[argv.index("--project") + 1], "improve-my-prompting")
        Check.true(SECRET_KEY not in " ".join(argv), "reason not redacted")


class Suite:
    """Builds the fixtures once and runs every test in order."""

    def run(self) -> int:
        # Runs the unit tests, then the integration tests that share one gather output.
        results = Results()
        base = Path(tempfile.mkdtemp(prefix="imp-test-"))
        try:
            home = Home(base)
            fake = FakeCli(base)
            units = UnitTests(home, base)
            for name, category in self.unit_cases():
                results.run(name, category, getattr(units, name))
            integration = IntegrationTests(home, fake, Runner(home, fake, base), base)
            for name, category in self.integration_cases():
                results.run(name, category, getattr(integration, name))
        finally:
            shutil.rmtree(str(base), ignore_errors=True)
        return results.finish()

    @staticmethod
    def unit_cases() -> list:
        # (method name, failure category) for every unit test.
        return [
            ("redactor_secrets", "Silent Failure"), ("redactor_none", "Hidden Assumption"), ("add_turn_dedupe", "Edge Case"), ("add_turn_blank", "Edge Case"),
            ("claude_injected", "Silent Failure"), ("claude_interrupt", "Hidden Failure"), ("claude_sdk", "Hidden Assumption"), ("claude_corrupt_line", "Hidden Failure"),
            ("codex_skips", "Hidden Assumption"), ("codex_reading", "Silent Failure"), ("opencode_v2", "Silent Failure"), ("opencode_v1", "Hidden Failure"),
            ("opencode_legacy", "Silent Failure"), ("opencode_no_v2", "Hidden Assumption"), ("opencode_bad_db", "Hidden Failure"), ("gemini_reading", "Edge Case"),
            ("gemini_truncated", "Hidden Failure"), ("openclaude_reading", "Silent Failure"), ("factory_reading", "Silent Failure"), ("pi_reading", "Silent Failure"),
            ("collector_only", "Edge Case"), ("collector_cap", "Silent Failure"), ("collector_bounds", "Edge Case"), ("collector_project_dir", "Silent Failure"),
            ("collector_empty", "Edge Case"), ("signals_correction", "Silent Failure"), ("signals_go_ahead", "Hidden Assumption"), ("signals_vague_opener", "Edge Case"),
            ("signals_drip", "Edge Case"), ("signals_emphasis", "Silent Failure"), ("signals_tricks", "Edge Case"), ("signals_empty", "Edge Case"),
            ("signals_ranking", "Silent Failure"), ("signals_templates", "Silent Failure"), ("digest_budget", "Silent Failure"), ("rubric_copy", "Hidden Assumption"),
            ("validator_problems", "Edge Case"), ("validator_untagged_kinds", "Hidden Assumption"), ("validator_redacts", "Silent Failure"), ("argv_shape", "Silent Failure"),
            ("cost_estimate", "Silent Failure"), ("rubric_index", "Hidden Assumption"), ("idea_reporter", "Silent Failure"),
        ]

    @staticmethod
    def integration_cases() -> list:
        # (method name, failure category) for every integration test, in dependency order.
        return [
            ("gather_end_to_end", "Hidden Failure"), ("gather_no_secrets", "Silent Failure"), ("gather_tags", "Hidden Failure"), ("gather_digest_small", "Edge Case"),
            ("gather_one_session", "Edge Case"), ("gather_nothing", "Edge Case"), ("current_session_excluded", "Hidden Assumption"), ("suggest_dry_run", "Silent Failure"),
            ("suggest_paid_once", "Silent Failure"), ("suggest_cli_error", "Hidden Failure"), ("suggest_no_cli", "Hidden Assumption"), ("report_after_paid", "Silent Failure"),
            ("feedback_accept", "Silent Failure"),
        ]


if __name__ == "__main__":
    sys.exit(Suite().run())
