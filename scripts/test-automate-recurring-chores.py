#!/usr/bin/env python3
"""Verification script for skills/automate-recurring-chores (design doc section 10).

Unit tests build sessionkit Session objects in memory and call the skill's modules directly.
Integration tests write Claude Code, Codex, and OpenCode stores into a temporary home and run
recurring_chores.py in subprocesses whose HOME, harness folders, and PATH all point into it. Every
suggest and feedback call goes to a recording fake vidbyte-cli passed with --cli, and PATH holds no
vidbyte-cli, so an ignored --cli fails with 127 instead of reaching a real (billing) CLI. The
routine runner is tested against fake claude and codex launchers. Run it with every Python you
support:

    python scripts/test-automate-recurring-chores.py
"""

from __future__ import annotations

import calendar
import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "automate-recurring-chores"
SCRIPTS = SKILL / "scripts"
ENTRY = SCRIPTS / "recurring_chores.py"
FOCUS = SKILL / "references" / "chore-routines.md"
sys.path.insert(0, str(SCRIPTS))

from sessionkit.collector import CollectOptions, SessionCollector  # noqa: E402
from sessionkit.model import Session  # noqa: E402
from sessionkit.redaction import Redactor  # noqa: E402
from sessionkit.runner import InputsValidator  # noqa: E402
from chores.cadence import CadenceAnalyzer  # noqa: E402
from chores.detector import AverageLinkage, ChoreDetector  # noqa: E402
from chores.digest import ChoreDigestWriter  # noqa: E402
from chores.report import RoutineReporter  # noqa: E402
from chores.routine import AllowedCommands, RoutineError, RoutineWriter  # noqa: E402
from chores.run_routine import CLAUDE_QUERY, BusinessCalendar, RunWindow  # noqa: E402
from chores.schedule import ScheduleBuilder  # noqa: E402
from chores.signatures import ActionParser, CommandSignature, PathNormalizer, SessionSignature, TemplatePrefixes, TermExtractor  # noqa: E402
from chores.suggest import FLAG_KINDS, TAG_PATTERN, TAGGED_KINDS, SuggestPlan  # noqa: E402

UTC = dt.timezone.utc
TODAY = dt.date.today()
SECRET_KEY = "sk-abcdefghijklmnopqrstuvwx1234"
SECRET_EMAIL = "bob.builder@example.com"
STRIP_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "VIDBYTE_API_KEY", "PYTHONPATH")
WATCHDOG = ("Inspect the build queue and report every job that is stuck or failing, with its id, the last step it ran, how long it has been idle, "
            "and whether it needs a restart. Do not change anything; only report the findings in a table, newest first, and stop.")
FAKE_CLI_SOURCE = r'''
import json, os, sys

def tags():
    # Chore tags by cadence kind, and each chore's latest session tag, from chores.json in the cwd.
    try:
        with open("chores.json", encoding="utf-8") as handle:
            index = json.load(handle)
    except (OSError, ValueError):
        return {}, {}
    by_kind = {c["cadence"]["kind"]: c["tag"] for c in index.get("chores", [])}
    first_session = {c["tag"]: (c["runs"][-1]["sessions"] or [""])[0] for c in index.get("chores", [])}
    return by_kind, first_session

def main():
    # Records the call, then answers like vidbyte-cli: an error, a suggestions result, or ok.
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
        kinds, sessions = tags()
        monthly, weekly = kinds.get("monthly", "C9"), kinds.get("weekly", "C8")
        good = ["ROUTINE PROMPT: Read the newest statements file for this month, categorize it with python scripts/categorize.py, and update the budget workbook. Stop and report if the file is missing.",
                "SCHEDULE: Windows Task Scheduler, daily trigger at 09:10 with -StartWhenAvailable; the script checks for the first business day.",
                "HUMAN STEPS: review the flagged transactions.", "DRY RUN: run it once and read the log.", "GO LIVE: after two clean dry runs set mode live.", "TURN OFF: Unregister-ScheduledTask."]
        ideas = [] if dry else [
            {"id": "idea-001", "rank": 1, "title": "[%s] Monthly budget cleanup: first-business-day routine" % monthly, "summary": "Runs [%s] on the first business day, as in [%s]." % (monthly, sessions.get(monthly, "")), "first_action": "Run the dry run.", "suggested_actions": good, "completion_criteria": "The dry run reads the statements.", "horizon": "now", "primary_category": "delegation"},
            {"id": "idea-002", "rank": 2, "title": "[%s] Invoices: monthly routine" % weekly, "summary": "Send the invoices for [%s] every month automatically." % weekly, "first_action": "Schedule it.", "suggested_actions": ["Make a cron job."], "completion_criteria": "Invoices go out.", "horizon": "next", "primary_category": "leverage"},
            {"id": "idea-003", "rank": 3, "title": "Automate more", "summary": "General advice with no tags.", "first_action": "Think.", "suggested_actions": ["Consider tools."], "completion_criteria": "Done.", "horizon": "later", "primary_category": "leverage"},
            {"id": "idea-004", "rank": 4, "title": "[%s] and [%s] together" % (monthly, weekly), "summary": "One routine for [%s] and [%s], with a human approval step and a dry run." % (monthly, weekly), "first_action": "Run the dry run.", "suggested_actions": good, "completion_criteria": "Both run.", "horizon": "later", "primary_category": "delegation"},
        ]
        usage = {"model_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0} if dry else {"model_calls": 5, "input_tokens": 300000, "cached_input_tokens": 100000, "output_tokens": 50000}
        data = {"status": "no_suggestions" if dry else "complete", "stop_reason": "dry_run" if dry else "completed", "settings": {"model": "gpt-6-sol", "critic_model": "gpt-6-sol"}, "usage": usage, "ideas": ideas, "context_manifest": [{"ref": "ctx-001", "kind": "file", "status": "included"}], "warnings": []}
        print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": data}))
        return 0
    print(json.dumps({"schema_version": 1, "kind": "ok", "data": {}}))
    return 0
'''
FAKE_AGENT_SOURCE = r'''
import json, os, sys, time

def main():
    # Records argv, cwd, and stdin, then prints a plan and exits with FAKE_AGENT_EXIT.
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"exe": os.path.basename(sys.argv[0]), "argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": data}) + "\n")
    if os.environ.get("FAKE_AGENT_SLEEP"):
        time.sleep(float(os.environ["FAKE_AGENT_SLEEP"]))
    print("PLAN: read the inputs and write the outputs.")
    return int(os.environ.get("FAKE_AGENT_EXIT", "0") or 0)
'''


class Results:
    """Collects PASS/FAIL per test and prints the summary."""

    def __init__(self) -> None:
        # Starts with no results.
        self.passed = 0
        self.failed: list = []

    def run(self, name: str, category: str, test) -> None:
        # Runs one test callable; any exception is a FAIL with its reason.
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
        # Prints "X/Y tests passed" and returns the exit code.
        total = self.passed + len(self.failed)
        print("\n%d/%d tests passed (Python %s)" % (self.passed, total, sys.version.split()[0]))
        return 0 if not self.failed else 1


class Check:
    """Assertion helpers with readable messages."""

    @staticmethod
    def equal(actual, expected, label: str = "") -> None:
        # Fails with both values when they differ.
        assert actual == expected, "%s expected %r, got %r" % (label, expected, actual)

    @staticmethod
    def true(value, label: str) -> None:
        # Fails with the label when value is falsy.
        assert value, label


class Dates:
    """Fixture dates relative to today, so cadences and next dates are always current."""

    @staticmethod
    def utc(day: dt.date, hour: int = 12, minute: int = 0) -> dt.datetime:
        # Noon UTC keeps the local date equal to the UTC date in every zone from UTC-11 to UTC+11.
        return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)

    @staticmethod
    def iso(stamp: dt.datetime) -> str:
        # An ISO string with milliseconds and Z, like Claude Code and Codex write.
        return stamp.strftime("%Y-%m-%dT%H:%M:%S") + ".123Z"

    @staticmethod
    def first_business_day(year: int, month: int) -> dt.date:
        # The first Monday-to-Friday day of a month.
        day = dt.date(year, month, 1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        return day

    @staticmethod
    def months_back(count: int) -> list:
        # The first business days of the `count` months before this month, oldest first.
        out = []
        index = TODAY.year * 12 + TODAY.month - 1
        for back in range(count, 0, -1):
            year, month = divmod(index - back, 12)
            out.append(Dates.first_business_day(year, month + 1))
        return out

    @staticmethod
    def mondays_back(count: int) -> list:
        # The `count` Mondays before this week, oldest first.
        monday = TODAY - dt.timedelta(days=TODAY.weekday())
        return [monday - dt.timedelta(days=7 * k) for k in range(count, 0, -1)]


class Mem:
    """In-memory sessions for unit tests."""

    @staticmethod
    def session(sid: str, start: dt.datetime, prompts: list, cwd: str = "/home/me/fin", actions: tuple = (), harness: str = "claude-code", final: str = "Done.") -> Session:
        # One session: prompts 5 minutes apart, the actions after the first prompt, then a final agent text.
        session = Session(harness, sid, "/mem/%s.jsonl" % sid)
        session.add_cwd(cwd)
        for index, text in enumerate(prompts):
            session.add_turn(text, start + dt.timedelta(minutes=5 * index))
            if index == 0:
                for action in actions:
                    session.note_action(action)
        session.note_agent_text(final, start + dt.timedelta(minutes=5 * len(prompts) + 10))
        return session.finish()

    @staticmethod
    def budget(days: list, cwd: str = "/home/me/fin", prefix: str = "b") -> list:
        # Monthly budget-cleanup sessions on the given days.
        out = []
        for k, day in enumerate(days):
            month = calendar.month_name[day.month]
            out.append(Mem.session("%s%d" % (prefix, k), Dates.utc(day, 12, 3 * k), [
                "Clean up my %s budget: categorize the transactions in statements/%04d-%02d.csv and update the budget totals" % (month, day.year, day.month),
                "flag anything over 200 dollars as unusual and list them for me"], cwd,
                ("Read %s/statements/%04d-%02d.csv" % (cwd, day.year, day.month), "Bash: python scripts/categorize.py statements/%04d-%02d.csv" % (day.year, day.month),
                 "Edit %s/budget/%04d-%02d.xlsx" % (cwd, day.year, day.month))))
        return out

    @staticmethod
    def noise(count: int, start: dt.date) -> list:
        # Unrelated daily coding sessions in other folders.
        topics = ["refactor the parser module", "fix the login redirect bug", "add pagination to the users endpoint", "write tests for the cache layer",
                  "update the readme install steps", "investigate the slow docker build", "rename the config loader class", "port the deploy script to typescript"]
        return [Mem.session("n%d" % k, Dates.utc(start + dt.timedelta(days=k), 15), ["%s in the web app and check edge case %d" % (topics[k % len(topics)], k)], "/home/me/web%d" % (k % 3)) for k in range(count)]


class Writer:
    """Writes fixture files with LF endings and sets their modification times."""

    @staticmethod
    def text(path: Path, text: str, stamp: dt.datetime = None) -> Path:
        # Writes text and optionally sets the mtime to stamp.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if stamp is not None:
            os.utime(str(path), (stamp.timestamp(), stamp.timestamp()))
        return path

    @staticmethod
    def jsonl(path: Path, records: list, stamp: dt.datetime) -> Path:
        # Writes JSON lines and sets the mtime.
        return Writer.text(path, "".join(json.dumps(r) + "\n" for r in records), stamp)


class ClaudeFixture:
    """Claude Code transcripts: prompts, tool calls (Read, Bash, Edit), and final answers."""

    @staticmethod
    def session(home: Path, sid: str, cwd: str, start: dt.datetime, prompts: list, tools: list, final: str = "Done; the totals are updated.") -> None:
        # Writes ~/.claude/projects/<slug>/<sid>.jsonl.
        records, minute = [], 0
        for index, text in enumerate(prompts):
            stamp = start + dt.timedelta(minutes=minute)
            records.append({"type": "user", "timestamp": Dates.iso(stamp), "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": text}})
            if index == 0 and tools:
                parts = [{"type": "tool_use", "id": "t%d" % n, "name": name, "input": args} for n, (name, args) in enumerate(tools)]
                records.append({"type": "assistant", "timestamp": Dates.iso(stamp + dt.timedelta(minutes=2)), "cwd": cwd, "message": {"role": "assistant", "model": "claude-opus-5-5", "content": parts, "stop_reason": None}})
                records.append({"type": "user", "timestamp": Dates.iso(stamp + dt.timedelta(minutes=3)), "cwd": cwd, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "ok"}]}})
            records.append({"type": "assistant", "timestamp": Dates.iso(stamp + dt.timedelta(minutes=8)), "cwd": cwd, "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": final}], "stop_reason": "end_turn"}})
            minute += 10
        Writer.jsonl(home / ".claude" / "projects" / "C--fixture" / ("%s.jsonl" % sid), records, start + dt.timedelta(minutes=minute))


class CodexFixture:
    """Codex rollouts: session_meta, prompts, shell calls, and final answers."""

    @staticmethod
    def session(home: Path, sid: str, cwd: str, start: dt.datetime, prompts: list, commands: list, final: str = "Drafted the invoices.") -> None:
        # Writes ~/.codex/sessions/YYYY/MM/DD/rollout-<sid>.jsonl.
        def rec(kind, payload, stamp):
            # One rollout record.
            return OrderedDict([("timestamp", Dates.iso(stamp)), ("type", kind), ("payload", payload)])
        records = [rec("session_meta", OrderedDict([("id", sid), ("cwd", cwd), ("originator", "codex_cli_rs"), ("source", "cli")]), start),
                   rec("turn_context", OrderedDict([("cwd", cwd), ("model", "gpt-6-sol")]), start)]
        minute = 1
        for index, text in enumerate(prompts):
            stamp = start + dt.timedelta(minutes=minute)
            records.append(rec("event_msg", OrderedDict([("type", "user_message"), ("message", text)]), stamp))
            if index == 0:
                for n, command in enumerate(commands):
                    records.append(rec("response_item", OrderedDict([("type", "function_call"), ("name", "shell_command"), ("arguments", json.dumps({"command": ["bash", "-lc", command]})), ("call_id", "c%d" % n)]), stamp + dt.timedelta(minutes=1)))
                    records.append(rec("response_item", OrderedDict([("type", "function_call_output"), ("call_id", "c%d" % n), ("output", "Exit code: 0\nok")]), stamp + dt.timedelta(minutes=2)))
            records.append(rec("event_msg", OrderedDict([("type", "agent_message"), ("message", final), ("phase", "final_answer")]), stamp + dt.timedelta(minutes=6)))
            records.append(rec("event_msg", OrderedDict([("type", "task_complete"), ("last_agent_message", final)]), stamp + dt.timedelta(minutes=6)))
            minute += 10
        folder = home / ".codex" / "sessions" / ("%04d" % start.year) / ("%02d" % start.month) / ("%02d" % start.day)
        Writer.jsonl(folder / ("rollout-%s.jsonl" % sid), records, start + dt.timedelta(minutes=minute))


class MessageLogFixture:
    """Factory Droid and Pi message logs (sessionkit v4), shaped like the improve-my-prompting fixtures."""

    @staticmethod
    def msg(stamp: dt.datetime, message: dict) -> dict:
        # One {"type": "message"} record.
        return {"type": "message", "id": "m%d" % stamp.timestamp(), "timestamp": Dates.iso(stamp), "message": message}

    @staticmethod
    def text(value: str) -> dict:
        # One text content part.
        return {"type": "text", "text": value}

    @staticmethod
    def factory(home: Path, sid: str, cwd: str, start: dt.datetime, prompt: str) -> None:
        # Writes ~/.factory/sessions/<slug>/<sid>.jsonl: a reminder-only message, the typed prompt, and a reply.
        m, t = MessageLogFixture.msg, MessageLogFixture.text
        records = [
            {"type": "session_start", "id": sid, "title": "New Session", "owner": "me", "version": 2, "cwd": cwd},
            m(start, {"role": "user", "visibility": "llm_only", "content": [t("<system-reminder>tools</system-reminder>")]}),
            m(start + dt.timedelta(seconds=5), {"role": "user", "content": [t(prompt)]}),
            m(start + dt.timedelta(minutes=3), {"role": "assistant", "content": [t("Done.")]}),
        ]
        Writer.jsonl(home / ".factory" / "sessions" / "-fixture" / ("%s.jsonl" % sid), records, start + dt.timedelta(minutes=3))

    @staticmethod
    def pi(home: Path, sid: str, cwd: str, start: dt.datetime, prompt: str) -> None:
        # Writes ~/.pi/agent/sessions/<slug>/<stamp>_<sid>.jsonl: a header, a model change, the prompt, and a finished reply.
        m, t = MessageLogFixture.msg, MessageLogFixture.text
        records = [
            {"type": "session", "version": 3, "id": sid, "timestamp": Dates.iso(start), "cwd": cwd},
            {"type": "model_change", "id": "x", "parentId": None, "timestamp": Dates.iso(start), "provider": "deepseek", "modelId": "deepseek-v4-pro"},
            m(start + dt.timedelta(seconds=5), {"role": "user", "content": [t(prompt)], "timestamp": int(start.timestamp() * 1000) + 5000}),
            m(start + dt.timedelta(minutes=2), {"role": "assistant", "model": "deepseek-v4-pro", "content": [t("Here it is.")], "stopReason": "stop"}),
        ]
        name = "%s_%s.jsonl" % (start.strftime("%Y-%m-%dT%H-%M-%S-000Z"), sid)
        Writer.jsonl(home / ".pi" / "agent" / "sessions" / "--fixture--" / name, records, start + dt.timedelta(minutes=2))


class Home:
    """A temporary home with a monthly Claude Code chore, a weekly Codex chore, an irregular repeat, a scheduled job, and noise."""

    def __init__(self, base: Path) -> None:
        # Builds every fixture under base/home.
        self.path = base / "home"
        self.fin = str(self.path / "finances")
        self.consulting = str(self.path / "consulting")
        self.monthly_days = Dates.months_back(5)
        self.weekly_days = Dates.mondays_back(6)
        self.build()

    def build(self) -> None:
        # Writes all fixtures.
        h = self.path
        for folder in (self.fin, self.consulting, str(h / "api"), str(h / "web0"), str(h / "ops")):
            Path(folder).mkdir(parents=True, exist_ok=True)
        for k, day in enumerate(self.monthly_days):
            month, stamp = calendar.month_name[day.month], "%04d-%02d" % (day.year, day.month)
            prompts = ["Clean up my %s budget: categorize the transactions in statements/%s.csv and update the budget totals in budget/%s.xlsx" % (month, stamp, stamp),
                       "flag anything over 200 dollars as unusual and list them for me"]
            if k == 1:
                prompts.append("Use STRIPE_API_KEY from .env (the key is %s) and mail the summary to %s; my folder is %s" % (SECRET_KEY, SECRET_EMAIL, str(h / "private-notes")))
            tools = [("Read", {"file_path": "%s/statements/%s.csv" % (self.fin, stamp)}), ("Bash", {"command": "python scripts/categorize.py statements/%s.csv" % stamp}),
                     ("Edit", {"file_path": "%s/budget/%s.xlsx" % (self.fin, stamp)})]
            ClaudeFixture.session(h, "budget-%d" % k, self.fin, Dates.utc(day, 12, 4 * k), prompts, tools)
            if k == 2:  # Finished the next day in a second session: still one run.
                ClaudeFixture.session(h, "budget-%d-b" % k, self.fin, Dates.utc(day + dt.timedelta(days=1), 12), ["Finish the %s budget cleanup: categorize the remaining transactions in statements/%s.csv and update the budget totals" % (month, stamp)], tools[:2])
        for k, day in enumerate(self.weekly_days):
            CodexFixture.session(h, "invoice-%d" % k, self.consulting, Dates.utc(day, 12, 5 * k), [
                "Generate this week's client invoices from the timesheet export in exports/timesheet-week.csv, draft the invoice PDFs, then email them to the clients"],
                ["python make_invoices.py exports/timesheet.csv", "git log --oneline -5"])
        for k, back in enumerate((61, 58, 41, 40, 13)):
            day = TODAY - dt.timedelta(days=back)
            ClaudeFixture.session(h, "deps-%d" % k, str(h / "api"), Dates.utc(day, 16, 13 * k), ["Update the dependency report for the api project and summarize the outdated packages by severity"], [("Bash", {"command": "npm outdated --json"})])
        watch_day = TODAY - dt.timedelta(days=3)
        for k in range(6):
            CodexFixture.session(h, "watch-%d" % k, str(h / "ops"), Dates.utc(watch_day, 6 + k, 0), [WATCHDOG], [])
        topics = ["refactor the parser module", "fix the login redirect bug", "add pagination to the users endpoint", "write tests for the cache layer", "update the readme install steps", "investigate the slow docker build"]
        for k, topic in enumerate(topics):
            ClaudeFixture.session(h, "noise-%d" % k, str(h / "web0"), Dates.utc(TODAY - dt.timedelta(days=5 + 9 * k), 15), ["%s in the web app and check edge case %d" % (topic, k)], [("Edit", {"file_path": "src/x%d.ts" % k})])
        self._opencode_legacy(h / ".local" / "share" / "opencode", str(h / "web0"), TODAY - dt.timedelta(days=2))
        web = str(h / "web0")
        Writer.jsonl(h / ".openclaude" / "projects" / "C--fixture" / "openclaude-1.jsonl", [
            {"type": "user", "timestamp": Dates.iso(Dates.utc(TODAY - dt.timedelta(days=4), 9)), "cwd": web, "sessionId": "openclaude-1", "message": {"role": "user", "content": "Rename the helper in src/util.ts to formatDate"}},
            {"type": "assistant", "timestamp": Dates.iso(Dates.utc(TODAY - dt.timedelta(days=4), 9, 3)), "cwd": web, "message": {"role": "assistant", "model": "x", "content": [{"type": "text", "text": "Renamed."}], "stop_reason": "end_turn"}},
        ], Dates.utc(TODAY - dt.timedelta(days=4), 9, 3))
        MessageLogFixture.factory(h, "fac-1", web, Dates.utc(TODAY - dt.timedelta(days=6), 11), "Fix the login redirect in app/auth.ts")
        MessageLogFixture.pi(h, "pi-1", web, Dates.utc(TODAY - dt.timedelta(days=7), 14), "List the files in src/ and explain the folder layout")

    @staticmethod
    def _opencode_legacy(data_dir: Path, cwd: str, day: dt.date) -> None:
        # One unrelated OpenCode session in the legacy storage/ tree.
        ms = int(Dates.utc(day, 10).timestamp() * 1000)
        storage = data_dir / "storage"
        Writer.text(storage / "session" / "proj" / "ses_legacy.json", json.dumps({"id": "ses_legacy", "directory": cwd, "title": "Legacy", "time": {"created": ms, "updated": ms + 60000}}))
        Writer.text(storage / "message" / "ses_legacy" / "msg_1.json", json.dumps({"id": "msg_1", "role": "user", "time": {"created": ms}}))
        Writer.text(storage / "part" / "msg_1" / "prt_1.json", json.dumps({"type": "text", "text": "explain how the router in src/routes.ts chooses a handler"}))


class Fakes:
    """Recording fake vidbyte-cli, claude, and codex launchers (real .exe files on Windows) made with pip's distlib."""

    def __init__(self, base: Path) -> None:
        # Writes the fake modules and builds the launchers.
        self.module_dir = base / "fakemods"
        Writer.text(self.module_dir / "fakecli.py", FAKE_CLI_SOURCE)
        Writer.text(self.module_dir / "fakeagent.py", FAKE_AGENT_SOURCE)
        self.cli_dir = base / "fakebin"
        self.agent_dir = base / "agentbin"
        self.cli_log = base / "fakecli-log.jsonl"
        self.agent_log = base / "fakeagent-log.jsonl"
        self.cli = self._make(self.cli_dir, "vidbyte-cli = fakecli:main")
        self.claude = self._make(self.agent_dir, "claude = fakeagent:main")
        self.codex = self._make(self.agent_dir, "codex = fakeagent:main")

    @staticmethod
    def _make(folder: Path, spec: str) -> str:
        # Builds one launcher and returns its path.
        from pip._vendor.distlib.scripts import ScriptMaker
        folder.mkdir(parents=True, exist_ok=True)
        maker = ScriptMaker(None, str(folder))
        maker.executable = sys.executable
        maker.variants = {""}
        return maker.make(spec)[0]

    @staticmethod
    def _read(path: Path) -> list:
        # Every recorded call.
        if not path.exists():
            return []
        with open(str(path), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def cli_calls(self) -> list:
        # Recorded vidbyte-cli calls: {"argv", "cwd"}.
        return self._read(self.cli_log)

    def agent_calls(self) -> list:
        # Recorded harness calls: {"exe", "argv", "cwd", "stdin"}.
        return self._read(self.agent_log)


class Runner:
    """Runs recurring_chores.py (or a routine's runner) in a subprocess confined to the fixture home."""

    def __init__(self, home: Home, fakes: Fakes, base: Path) -> None:
        # Builds the confined environment once.
        self.home = home
        self.fakes = fakes
        self.env = self._env(base)

    def run(self, *args: str, extra_env: dict = None, script: Path = ENTRY):
        # Returns (exit code, parsed stdout JSON or None, stdout, stderr).
        env = dict(self.env)
        env.update(extra_env or {})
        process = subprocess.run([sys.executable, str(script)] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        out = process.stdout.decode("utf-8", "replace")
        try:
            document = json.loads(out) if out.strip() else None
        except ValueError:
            document = None
        return process.returncode, document, out, process.stderr.decode("utf-8", "replace")

    def _env(self, base: Path) -> dict:
        # HOME and every harness folder in the fixture home; PATH holds the fake agents and no vidbyte-cli.
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        h = self.home.path
        env.update({
            "HOME": str(h), "USERPROFILE": str(h), "CLAUDE_CONFIG_DIR": str(h / ".claude"), "CODEX_HOME": str(h / ".codex"),
            "XDG_DATA_HOME": str(h / ".local" / "share"), "APPDATA": str(h / "AppData" / "Roaming"), "LOCALAPPDATA": str(h / "AppData" / "Local"),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(base / "vb-state"), "FAKE_CLI_LOG": str(self.fakes.cli_log), "FAKE_AGENT_LOG": str(self.fakes.agent_log),
            "PYTHONPATH": str(self.fakes.module_dir), "PYTHONIOENCODING": "utf-8", "PATH": os.pathsep.join([str(self.fakes.agent_dir), self.clean_path()]),
        })
        return env

    @staticmethod
    def clean_path() -> str:
        # The current PATH minus every folder that holds a vidbyte-cli, claude, or codex.
        keep = []
        names = [n + ext for n in ("vidbyte-cli", "claude", "codex") for ext in ("", ".exe", ".cmd", ".bat", ".ps1")]
        for folder in os.environ.get("PATH", "").split(os.pathsep):
            if folder and not any(os.path.exists(os.path.join(folder, n)) for n in names):
                keep.append(folder)
        return os.pathsep.join(keep)


@contextlib.contextmanager
def only_path(folder: Path):
    # Temporarily sets PATH to one folder, so shutil.which finds only the fake agents.
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = str(folder)
    try:
        yield
    finally:
        os.environ["PATH"] = saved


class UnitTests:
    """Section 10 unit tests."""

    def __init__(self, base: Path, fakes: Fakes) -> None:
        # Keeps a scratch folder and the fake launchers.
        self.base = base
        self.fakes = fakes
        self.extractor = TermExtractor()
        self.paths = PathNormalizer()
        self.commands = CommandSignature(self.paths)
        self.parser = ActionParser(self.paths, self.commands)

    def terms_periods(self) -> None:
        # Month names, years, dates, and quarter labels are removed from terms and reported as period mentions.
        Check.equal(self.extractor.terms("the September invoices"), self.extractor.terms("the October invoices"), "terms")
        Check.equal(self.extractor.terms("Q3 2026-09-01 invoices"), self.extractor.terms("invoices"), "periods removed")
        Check.equal(self.extractor.period_mentions("Q3 2026 invoices for September, 2026-09-01"), ["month", "date", "year", "quarter"])

    def terms_empty(self) -> None:
        # Empty and whitespace text give no terms.
        Check.equal(self.extractor.terms(""), frozenset())
        Check.equal(self.extractor.terms("   \n "), frozenset())
        Check.equal(self.extractor.terms(None), frozenset())

    def terms_no_secret(self) -> None:
        # A credential value or email in a prompt never becomes a term.
        terms = self.extractor.terms("export STRIPE_API_KEY=sk_live_abcdefghijklmnop and mail %s" % SECRET_EMAIL)
        Check.true(not any("abcdefghijklmnop" in t or "example" in t for t in terms), "secret in terms: %r" % sorted(terms))

    def paths_normalized(self) -> None:
        # Windows and POSIX paths inside the session folder normalize to the same placeholder form.
        Check.equal(self.paths.normalize("C:\\Users\\me\\fin\\statements\\2026-09.csv", "C:\\Users\\me\\fin"), "statements/<date>.csv")
        Check.equal(self.paths.normalize("/home/me/fin/statements/2026-10.csv", "/home/me/fin"), "statements/<date>.csv")
        Check.equal(self.paths.normalize("reports/q3/September-summary.pdf", ""), "reports/<quarter>/<month>-summary.pdf")
        Check.equal(self.paths.normalize("C:/Users/me/a/very/deep/folder/statements/2026-…", ""), "", "a path cut by sessionkit names no file")

    def command_signatures(self) -> None:
        # cd prefixes, env assignments, and generic commands are handled; data-file arguments are reads.
        Check.equal(self.commands.of("cd app && npm outdated --json").signature, "npm outdated")
        Check.equal(self.commands.of("FOO=1 python scripts/cat.py").signature, "python scripts/cat.py")
        Check.equal(self.commands.of("ls -la").signature, "")
        Check.equal(self.commands.of("cat data/2026-09.csv").reads, ["data/<date>.csv"])

    def command_external(self) -> None:
        # git push, gh pr create, and curl POST are outside actions; git pull and a plain curl GET are not.
        for command in ("git push origin main", "gh pr create --fill", "curl -X POST https://api.example/x -d a=1"):
            Check.true(self.commands.of(command).external, "%s not external" % command)
        for command in ("git pull", "curl https://example.com/x"):
            Check.equal(self.commands.of(command).external, [], command)

    def action_parser(self) -> None:
        # Every sessionkit action shape maps to its kind; an unknown shape is "other".
        Check.equal(self.parser.parse("Bash: npm test").kind, "command")
        Check.equal(self.parser.parse("Edit src/x.py", "").writes, ["src/x.py"])
        Check.equal(self.parser.parse("Read notes/a.md", "").reads, ["notes/a.md"])
        web = self.parser.parse("WebFetch https://www.bank.example/statements?id=7")
        Check.equal((web.kind, web.domain), ("web", "bank.example"))
        mcp = self.parser.parse("mcp__gmail__send_message %s" % SECRET_EMAIL)
        Check.equal((mcp.kind, mcp.mcp), ("mcp", "gmail:send_message"))
        Check.true(mcp.external, "an MCP send tool is not an outside action")
        Check.equal(self.parser.parse("Frobnicate z").kind, "other")

    def signature_credentials(self) -> None:
        # Credential names are captured; the value never appears in any field.
        session = Mem.session("c", Dates.utc(TODAY), ["export STRIPE_API_KEY=sk_live_abcdefghijklmnop then read .env and run the export"], actions=("Bash: STRIPE_API_KEY=sk_live_abcdefghijklmnop python pull.py",))
        signature = SessionSignature(session, self.extractor, self.parser)
        Check.true("STRIPE_API_KEY" in signature.credentials and ".env file" in signature.credentials, "credentials: %r" % signature.credentials)
        fields = json.dumps([sorted(signature.terms), signature.credentials, sorted(signature.commands), sorted(signature.reads), signature.external, sorted(signature.resources)])
        Check.true("abcdefghijklmnop" not in fields, "a credential value leaked into the signature")

    def signature_openers(self) -> None:
        # A continuation opener is not a fresh run; a bare greeting gives way to the next prompt.
        cont = SessionSignature(Mem.session("x", Dates.utc(TODAY), ["continue where we left off"]), self.extractor, self.parser)
        Check.true(cont.continuation, "continuation not detected")
        hi = SessionSignature(Mem.session("y", Dates.utc(TODAY), ["hi", "categorize the September transactions in statements.csv"]), self.extractor, self.parser)
        Check.equal(hi.opener_index, 1, "opener index")
        Check.true(not hi.continuation, "a real request is not a continuation")

    def template_prefix(self) -> None:
        # A long template pasted before different tasks is stripped; identical full prompts are not.
        template = "Follow these engineering guidelines strictly. " * 6
        texts = [template + task for task in ("fix the login bug", "add a csv export", "rename the cache class", "write the release notes")]
        prefixes = TemplatePrefixes(texts + ["same prompt " * 30] * 4)
        Check.equal(len(prefixes.prefixes), 1, "templates")
        Check.equal(prefixes.strip(texts[1])[0], "add a csv export", "task text")
        Check.equal(prefixes.strip("same prompt " * 30)[1], 0, "identical prompts stripped")

    def skill_body(self) -> None:
        # A prompt holding a skill body ("Base directory for this skill:") becomes the skill's /name.
        body = "# speak-clear\n\n## Core principle\nWrite plainly.\n\nBase directory for this skill: /home/me/.skills/speak-clear\nRelative paths in this skill (e.g., scripts/) are relative to this base directory."
        alone = SessionSignature(Mem.session("s", Dates.utc(TODAY), [body]), self.extractor, self.parser)
        Check.equal((alone.skill_name, alone.task_text), ("/speak-clear", "/speak-clear"), "skill run")
        followed = SessionSignature(Mem.session("t", Dates.utc(TODAY), [body, "rewrite the onboarding email so it is shorter"]), self.extractor, self.parser)
        Check.equal(followed.opener_index, 1, "the typed request after the skill body is the opener")
        Check.true(not {"relative", "base", "directory", "principle"} & followed.terms, "skill wrapper words became terms: %r" % sorted(followed.terms))

    def cadence_first_business_day(self) -> None:
        # Jun 1, Jul 1, Aug 3, Sep 1, Oct 1 2026 are first business days; next is Nov 2 2026.
        days = [dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 3), dt.date(2026, 9, 1), dt.date(2026, 10, 1)]
        cadence = CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 10, 13))
        Check.equal((cadence.kind, cadence.pattern), ("monthly", "first_business_day"))
        Check.equal(cadence.next_expected, dt.date(2026, 11, 2), "next")
        Check.equal(cadence.overdue, False, "overdue")
        Check.true(cadence.label().startswith("monthly, on the first business day"), cadence.label())

    def cadence_weekly(self) -> None:
        # Four Mondays are weekly with regularity 1; one skipped week stays weekly below 1.
        monday = dt.date(2026, 9, 7)
        days = [monday + dt.timedelta(days=7 * k) for k in range(4)]
        cadence = CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 10, 13))
        Check.equal((cadence.kind, cadence.weekday, cadence.regularity), ("weekly", 0, 1.0))
        skipped = [monday, monday + dt.timedelta(days=7), monday + dt.timedelta(days=21), monday + dt.timedelta(days=28)]
        cadence = CadenceAnalyzer().analyze(skipped, [Dates.utc(d) for d in skipped], dt.date(2026, 10, 13))
        Check.true(cadence.kind == "weekly" and cadence.regularity < 1.0, "%s %s" % (cadence.kind, cadence.regularity))

    def cadence_weekdays(self) -> None:
        # Ten consecutive weekdays are "every weekday", not daily.
        days = [d for d in (dt.date(2026, 9, 7) + dt.timedelta(days=k) for k in range(14)) if d.weekday() < 5]
        Check.equal(CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 9, 30)).kind, "weekdays")

    def cadence_irregular(self) -> None:
        # Uneven gaps are irregular.
        base = dt.date(2026, 6, 1)
        days = [base + dt.timedelta(days=n) for n in (0, 2, 19, 20, 39)]
        Check.equal(CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 9, 30)).kind, "irregular")

    def cadence_patterns(self) -> None:
        # Last business days and a quarterly mid-month pattern are recognized.
        days = [dt.date(2025, 7, 31), dt.date(2025, 8, 29), dt.date(2025, 9, 30)]
        Check.equal(CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2025, 10, 5)).pattern, "last_business_day")
        days = [dt.date(2026, 1, 15), dt.date(2026, 4, 15), dt.date(2026, 7, 15), dt.date(2026, 10, 15)]
        cadence = CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 10, 20))
        Check.equal((cadence.kind, cadence.month_in_quarter, cadence.day), ("quarterly", 1, 15))
        Check.equal(cadence.next_expected, dt.date(2027, 1, 15), "next quarterly")

    def cadence_too_few(self) -> None:
        # Two runs are too few; a daily pattern needs 5 runs.
        days = [dt.date(2026, 9, 1), dt.date(2026, 10, 1)]
        Check.equal(CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 10, 5)).kind, "irregular")
        days = [dt.date(2026, 9, 5) + dt.timedelta(days=k) for k in range(4)]
        Check.equal(CadenceAnalyzer().analyze(days, [Dates.utc(d) for d in days], dt.date(2026, 9, 10)).kind, "irregular")

    def cadence_overdue(self) -> None:
        # Inside the catch-up window is not overdue; a whole missed period is.
        days = [dt.date(2026, 9, 7) + dt.timedelta(days=7 * k) for k in range(4)]  # Mondays to Sep 28
        starts = [Dates.utc(d) for d in days]
        Check.equal(CadenceAnalyzer().analyze(days, starts, dt.date(2026, 10, 6)).overdue, False, "Tuesday after the expected Monday")
        Check.equal(CadenceAnalyzer().analyze(days, starts, dt.date(2026, 10, 13)).overdue, True, "a full week later")

    def window_first_business_day(self) -> None:
        # Business days 1-3 are the window; weekends and later days are not; periods differ across months.
        window = RunWindow({"kind": "monthly", "pattern": "first_business_day"})
        Check.equal([window.in_window(dt.date(2026, 11, d)) for d in (1, 2, 4, 6)], [False, True, True, False])
        Check.true(window.period_key(dt.date(2026, 11, 2)) != window.period_key(dt.date(2026, 12, 1)), "period keys")

    def window_clamp_biweekly(self) -> None:
        # Day 31 clamps to Feb 28; biweekly alternates weeks from the anchor.
        window = RunWindow({"kind": "monthly", "pattern": "day_of_month", "day": 31})
        Check.equal((window.in_window(dt.date(2026, 2, 28)), window.in_window(dt.date(2026, 2, 27))), (True, False))
        biweekly = RunWindow({"kind": "biweekly", "weekday": 0, "anchor": "2026-09-07"})
        Check.equal([biweekly.in_window(dt.date(2026, 9, d)) for d in (7, 14, 21)], [True, False, True])

    def window_triggers(self) -> None:
        # Monthly and quarterly triggers never restrict the day of week (cron ORs the two fields).
        for config in ({"kind": "monthly", "pattern": p, "day": 10} for p in ("first_business_day", "last_business_day", "end_of_month", "day_of_month")):
            triggers = RunWindow(config).triggers()
            Check.true(triggers["type"] == "monthly" and "weekdays" not in triggers, "%s: %r" % (config["pattern"], triggers))
        Check.equal(RunWindow({"kind": "monthly", "pattern": "first_business_day"}).triggers()["days"], [1, 2, 3, 4, 5])

    def detector_monthly_chore(self) -> None:
        # Four monthly budget sessions form one chore; an unrelated daily coding stream does not join it.
        days = [dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 3), dt.date(2026, 9, 1)]
        result = ChoreDetector(today=dt.date(2026, 9, 20)).detect(Mem.budget(days) + Mem.noise(30, dt.date(2026, 6, 1)))
        Check.equal(len(result.chores), 1, "chores")
        chore = result.chores[0]
        Check.equal({s.session.session_id for s in chore.signatures}, {"b0", "b1", "b2", "b3"}, "members")
        Check.equal((chore.cadence.kind, chore.cadence.pattern), ("monthly", "first_business_day"))
        Check.equal(chore.inputs["files_read"][0], ["statements/<date>.csv", 4], "inputs")
        Check.equal(chore.label_signature.session.session_id, "b3", "the label comes from the latest run on a tie")

    def detector_same_day(self) -> None:
        # Three similar sessions on one day are one run: no chore.
        day = dt.date(2026, 9, 1)
        sessions = [Mem.session("d%d" % k, Dates.utc(day, 9 + k), ["Clean up the budget: categorize the transactions in statements.csv and update totals"]) for k in range(3)]
        result = ChoreDetector(today=dt.date(2026, 9, 2)).detect(sessions)
        Check.equal((len(result.chores), len(result.automated)), (0, 0))

    def detector_next_day(self) -> None:
        # A run finished the next day in a second session is one run, not two.
        days = [dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 3), dt.date(2026, 9, 1)]
        extra = Mem.budget([dt.date(2026, 7, 2)], prefix="late")
        result = ChoreDetector(today=dt.date(2026, 9, 20)).detect(Mem.budget(days) + extra)
        Check.equal(len(result.chores), 1, "chores")
        Check.equal(len(result.chores[0].runs), 4, "runs")
        Check.equal(result.chores[0].cadence.kind, "monthly")

    def detector_no_chaining(self) -> None:
        # Average linkage: A~B and B~C with A unlike C never chain into one cluster of three.
        clusters = AverageLinkage().cluster(3, {(0, 1): 0.9, (1, 2): 0.9}, 0.5)
        Check.true(max(len(c) for c in clusters) == 2, "chained: %r" % clusters)

    def detector_empty(self) -> None:
        # Zero or one session gives no chores and no error.
        Check.equal(len(ChoreDetector().detect([]).chores), 0)
        Check.equal(len(ChoreDetector().detect(Mem.budget([dt.date(2026, 9, 1)])).chores), 0)

    def detector_external(self) -> None:
        # A chore's outside action ("email them") is recorded with its prompt as evidence.
        days = [dt.date(2026, 9, 7) + dt.timedelta(days=7 * k) for k in range(5)]
        sessions = [Mem.session("i%d" % k, Dates.utc(d, 12, 7 * k), ["Generate this week's client invoices from exports/timesheet.csv, draft the PDFs, then email them to the clients"], "/home/me/consulting", ("Bash: python make_invoices.py exports/timesheet.csv",)) for k, d in enumerate(days)]
        result = ChoreDetector(today=dt.date(2026, 10, 9)).detect(sessions)
        Check.equal(len(result.chores), 1, "chores")
        actions = result.chores[0].outputs["external_actions"]
        Check.true(actions and actions[0]["action"] == "asked to send" and actions[0]["runs"] == 5 and actions[0]["evidence"], "external: %r" % actions)

    def detector_template(self) -> None:
        # Sessions that share only a pasted template, with different tasks, are not a chore.
        template = "Follow these engineering guidelines strictly and keep every change small and reviewed. " * 4
        tasks = ["fix the login redirect bug", "add a csv export to reports", "rename the cache loader class", "write the release notes draft", "port the deploy script", "speed up the docker build"]
        sessions = [Mem.session("t%d" % k, Dates.utc(dt.date(2026, 9, 1) + dt.timedelta(days=k)), [template + tasks[k]], "/home/me/app") for k in range(6)]
        result = ChoreDetector(today=dt.date(2026, 9, 20)).detect(sessions)
        Check.equal(len(result.chores), 0, "chores")
        Check.equal(len(result.templates), 1, "templates")

    def detector_automated(self) -> None:
        # The same long first prompt every hour is a scheduled job: listed as already automated, never a chore.
        day = dt.date(2026, 9, 25)
        sessions = [Mem.session("w%d" % k, Dates.utc(day, 1 + k), [WATCHDOG], "/home/me/ops") for k in range(8)]
        sessions += [Mem.session("v%d" % k, Dates.utc(day + dt.timedelta(days=1), 1 + k), [WATCHDOG], "/home/me/ops") for k in range(8)]
        result = ChoreDetector(today=dt.date(2026, 9, 28)).detect(sessions)
        Check.equal((len(result.chores), len(result.automated)), (0, 1))
        evidence = " ".join(result.automated[0].automation_evidence)
        Check.true("word for word" in evidence and "every 60 minutes" in evidence, evidence)

    def detector_identical_short(self) -> None:
        # A short saved prompt typed by hand each month (varied times) is still a chore.
        days = [dt.date(2026, 5, 4), dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 3), dt.date(2026, 9, 1)]
        sessions = [Mem.session("m%d" % k, Dates.utc(d, 9 + k, 11 * k), ["Run the monthly expense categorization for the shared ledger"], "/home/me/ledger") for k, d in enumerate(days)]
        result = ChoreDetector(today=dt.date(2026, 9, 10)).detect(sessions)
        Check.equal((len(result.chores), len(result.automated)), (1, 0))

    def digest_budget_and_tags(self) -> None:
        # The digest never exceeds max_chars; every tag in digest.md and inputs.json exists in chores.json.
        tasks = ["Reconcile the payroll timesheets with overtime approvals and update the salary ledger",
                 "Count warehouse inventory stock levels and reorder anything below the minimum quantity",
                 "Categorize credit card expenses by vendor and flag duplicate charges in the expense sheet",
                 "Summarize customer support tickets by product area and escalate unresolved complaints",
                 "Refresh the sales pipeline forecast from crm opportunities and highlight stalled deals",
                 "Rotate server access logs, compress the archives, and check disk usage on the backup volume",
                 "Compile the newsletter draft from blog posts, product updates, and community highlights",
                 "Audit dependency licenses in the monorepo and list packages with copyleft terms",
                 "Prepare grant compliance figures from lab spending and equipment purchase receipts",
                 "Review analytics dashboards for traffic anomalies and summarize conversion funnel drops"]
        sessions = []
        for c, task in enumerate(tasks):
            days = [dt.date(2026, 3, 2) + dt.timedelta(days=7 * k + c % 5) for k in range(20)]
            for k, day in enumerate(days):
                text = "%s, batch %d. Keep the notes short and list every open question at the end." % (task, k + 1)
                sessions.append(Mem.session("q%d-%d" % (c, k), Dates.utc(day, 10, 2 * k), [text, "also attach the variance table and note anything unusual in batch %d" % (k + 1)], "/home/me/q%d" % c, ("Read /home/me/q%d/exports/records.csv" % c,), final="Finished the batch. " * 40))
        result = ChoreDetector(today=dt.date(2026, 7, 30)).detect(sessions)
        Check.true(len(result.chores) >= 8, "chores: %d" % len(result.chores))
        out = self.base / "digest-budget"
        out.mkdir(parents=True, exist_ok=True)
        summary = ChoreDigestWriter(out, Redactor(Path("/home/me")), 20000, FOCUS, Path("/home/me")).write(result, {"kept": len(sessions), "kept_by_harness": {"claude-code": len(sessions)}}, {"platform": "Linux", "scheduler": "cron", "headless_clis": {"claude": True}})
        digest = (out / "digest.md").read_text(encoding="utf-8")
        Check.true(len(digest) <= 20000 and summary["digest_chars"] <= 20000, "digest has %d chars" % len(digest))
        index = json.loads((out / "chores.json").read_text(encoding="utf-8"))
        known = set(index["digest_tags"])
        indexed = {c["tag"] for c in index["chores"]} | {s["tag"] for s in index["sessions"]} | {p["tag"] for p in index["prompts"]}
        Check.true(known <= indexed, "digest tags missing from the index: %r" % sorted(known - indexed))
        inputs = json.loads((out / "inputs.json").read_text(encoding="utf-8"))
        import re
        cited = {t for kind in ("outcomes", "context", "risks") for v in inputs[kind] for t in re.findall(r"\b[CSP]\d+\b", v.split("]", 1)[0])}
        Check.true(cited <= known, "inputs cite tags not in the digest: %r" % sorted(cited - known))
        Check.equal(len({s["session_id"] for s in index["sessions"]}), len(index["sessions"]), "unique session tags")
        Check.true(all(v.startswith("[all]") for v in inputs["forbidden"]), "forbidden values must start with [all]")

    def digest_focus_copy(self) -> None:
        # The focus file is under 20,000 bytes and copied byte for byte.
        out = self.base / "digest-budget"
        Check.true(FOCUS.stat().st_size <= 20000, "focus file is %d bytes" % FOCUS.stat().st_size)
        Check.equal((out / "chore-routines.md").read_bytes(), FOCUS.read_bytes(), "focus copy")

    def digest_login_name(self) -> None:
        # Key terms that redaction would change (here the login name) never reach the digest.
        days = [dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 3), dt.date(2026, 9, 1)]
        sessions = [Mem.session("l%d" % k, Dates.utc(d, 12, k), ["Back up the zorblat ledger from /home/zorblat/ledger into the archive and verify zorblat checksums"], "/home/zorblat/ledger") for k, d in enumerate(days)]
        result = ChoreDetector(today=dt.date(2026, 9, 5)).detect(sessions)
        out = self.base / "digest-login"
        out.mkdir(parents=True, exist_ok=True)
        ChoreDigestWriter(out, Redactor(Path("/home/zorblat")), 60000, FOCUS, Path("/home/zorblat")).write(result, {"kept": 4}, {"platform": "Linux", "scheduler": "cron", "headless_clis": {}})
        for name in ("digest.md", "inputs.json"):
            Check.true("zorblat" not in (out / name).read_text(encoding="utf-8").lower(), "login name in %s" % name)

    def suggest_count(self) -> None:
        # One idea per chore, between 2 and 10.
        Check.equal((SuggestPlan.count(0), SuggestPlan.count(1), SuggestPlan.count(4), SuggestPlan.count(14)), (2, 2, 4, 10))

    def validator_problems(self) -> None:
        # Unknown keys, too many values, long values, untagged values, and unknown tags are problems; avoid needs no tag.
        validator = InputsValidator(FLAG_KINDS, TAGGED_KINDS, TAG_PATTERN, {"C1", "S1", "P1"}, Redactor(Path("/home/me")), required_any=("outcomes",))
        data = {"goal": "g", "bogus": 1, "outcomes": ["[C1 S1] ok"] * 11, "risks": ["untagged risk", "[C9] unknown chore", "[C1 P1] " + "x" * 600], "avoid": ["plain words are fine"]}
        _, problems = validator.validate(data)
        text = "\n".join(problems)
        for needle in ("Unknown key 'bogus'", "outcomes has 11 values", "must start with a bracketed digest tag", "names C9", "keep each value under 500"):
            Check.true(needle in text, "missing problem %r in %s" % (needle, text))
        Check.true(not any(line.startswith("Every avoid value") for line in problems), "avoid needs no tag")
        _, ok = validator.validate({"goal": "g", "outcomes": ["[C1 S1] fine"], "forbidden": ["[all] never send"]})
        Check.equal(ok, [], "valid inputs")

    def reporter_flags(self) -> None:
        # Tags resolve through sessions; ungrounded, cadence mismatch, unguarded outside action, merged, and uncovered are flagged.
        out = self.base / "report-unit"
        out.mkdir(parents=True, exist_ok=True)
        chores = {"chores": [
            {"tag": "C1", "label": "budget", "cadence": {"kind": "monthly", "label": "monthly, on the first business day"}, "runs": [{"day": "2026-09-01", "sessions": ["S3"]}], "prompts": ["P4"], "outputs": {"external_actions": []}},
            {"tag": "C2", "label": "invoices", "cadence": {"kind": "weekly", "label": "weekly, on Mondays"}, "runs": [{"day": "2026-09-07", "sessions": ["S5"]}], "prompts": ["P6"], "outputs": {"external_actions": [{"action": "asked to send", "runs": 3, "tags": ["P6"]}]}},
            {"tag": "C3", "label": "backups", "cadence": {"kind": "daily", "label": "daily"}, "runs": [], "prompts": [], "outputs": {}},
        ], "sessions": [{"tag": "S3", "harness": "claude-code", "resume": {"command": "claude --resume abc", "env": {}}}], "already_automated": [{"label": "watchdog", "runs": 2, "days": ["2026-09-25"], "automation_evidence": ["the sessions start every 60 minutes"]}], "irregular": []}
        ideas = [
            {"id": "idea-001", "rank": 1, "title": "Budget routine", "summary": "Runs on the first business day of each month as in [S3].", "suggested_actions": ["ROUTINE PROMPT: do it", "SCHEDULE: cron", "HUMAN STEPS: review", "DRY RUN: once"]},
            {"id": "idea-002", "rank": 2, "title": "[C2] Invoices: monthly routine", "summary": "Send the invoices automatically.", "suggested_actions": ["Make a cron job."]},
            {"id": "idea-003", "rank": 3, "title": "Automate more", "summary": "No tags here.", "suggested_actions": ["Think."]},
            {"id": "idea-004", "rank": 4, "title": "[C1] [C2] together", "summary": "Both, with a human approval and a dry run.", "suggested_actions": ["x"]},
        ]
        Writer.text(out / "chores.json", json.dumps(chores))
        Writer.text(out / "result.json", json.dumps({"kind": "suggestions.result", "data": {"ideas": ideas}}))
        report = RoutineReporter(out, Redactor(Path("/home/me"))).build()
        by_id = {i["id"]: i for i in report["ideas"]}
        Check.equal(by_id["idea-001"]["chores"], ["C1"], "S3 resolves to C1")
        Check.equal(by_id["idea-001"]["missing"], [], "missing")
        flags2 = " ".join(by_id["idea-002"]["flags"])
        Check.true("cadence_mismatch" in flags2 and "unguarded_external_action" in flags2, flags2)
        Check.true("grounded: false" in by_id["idea-003"]["flags"], "ungrounded not flagged")
        Check.true(any(f.startswith("merged_chores") for f in by_id["idea-004"]["flags"]), "merged not flagged")
        Check.true(any(f.startswith("duplicate_chore") for f in by_id["idea-004"]["flags"]), "duplicate not flagged")
        Check.equal([c["tag"] for c in report["uncovered_chores"]], ["C3"], "uncovered")
        markdown = (out / "report.md").read_text(encoding="utf-8")
        Check.true("claude --resume abc" in markdown and "already automated" in markdown, "report.md lacks resume or automated sections")

    def reporter_ignores_evidence(self) -> None:
        # handoff.evidence content (the digest's top) is never read for tags.
        out = self.base / "report-evidence"
        out.mkdir(parents=True, exist_ok=True)
        Writer.text(out / "chores.json", json.dumps({"chores": [{"tag": "C1", "label": "x", "cadence": {"kind": "weekly"}, "runs": [], "prompts": [], "outputs": {}}]}))
        idea = {"id": "idea-001", "rank": 1, "title": "Untagged", "summary": "s", "suggested_actions": ["a"], "handoff": {"evidence": [{"content": "### [C1] x"}]}}
        Writer.text(out / "result.json", json.dumps({"kind": "suggestions.result", "data": {"ideas": [idea]}}))
        Check.equal(RoutineReporter(out, Redactor(Path("/home/me"))).build()["ideas"][0]["grounded"], False, "grounded")

    def schedule_builder(self) -> None:
        # Cron, launchd, and Windows text follow the window's triggers.
        runner = Path("/home/me/routines/budget/run_routine.py")
        first = ScheduleBuilder(RunWindow({"kind": "monthly", "pattern": "first_business_day"}), "09:10", "/usr/bin/python3", runner, "c1-budget")
        Check.true(first.cron_line().startswith("10 9 1-5 * * "), first.cron_line())
        plist = first.launchd_plist()
        Check.true(all("<key>Day</key>\n\t\t\t<integer>%d</integer>" % d in plist for d in range(1, 6)) and "<integer>6</integer>" not in plist.split("StartCalendarInterval")[1].split("StandardOutPath")[0].replace("<integer>10</integer>", ""), "plist days")
        windows = first.windows_commands()
        Check.true("-Daily -At 09:10" in windows["powershell"] and "-StartWhenAvailable" in windows["powershell"] and "/SC DAILY /ST 09:10" in windows["schtasks"], windows["powershell"])
        weekly = ScheduleBuilder(RunWindow({"kind": "weekly", "weekday": 0}), "09:10", "/usr/bin/python3", runner, "c2-inv")
        Check.true(weekly.cron_line().startswith("10 9 * * 1-3 "), weekly.cron_line())
        Check.true("-Weekly -DaysOfWeek Monday,Tuesday,Wednesday" in weekly.windows_commands()["powershell"] and "/SC WEEKLY /D MON,TUE,WED" in weekly.windows_commands()["schtasks"], "weekly windows")
        Check.true("<key>Weekday</key>\n\t\t\t<integer>1</integer>" in weekly.launchd_plist(), "launchd Monday is 1")
        quarterly = ScheduleBuilder(RunWindow({"kind": "quarterly", "month_in_quarter": 1, "day": 15}), "08:00", "/usr/bin/python3", runner, "c3-tax")
        Check.true(quarterly.cron_line().startswith("0 8 15-21 1,4,7,10 * "), quarterly.cron_line())
        sunday = ScheduleBuilder(RunWindow({"kind": "weekly", "weekday": 6}), "07:00", "/usr/bin/python3", runner, "c4")
        Check.true(sunday.cron_line().startswith("0 7 * * 0 "), sunday.cron_line())

    def allowed_commands(self) -> None:
        # Outside actions, destructive commands, network senders, inline code, and placeholder paths are never pre-approved.
        picked = AllowedCommands.pick(["python scripts/categorize.py", "git push", "gh pr create", "curl", "rm", "stripe invoices create", "python (inline code)", "python scripts/<date>/x.py", "git log", "git reset", "gh pr list", "gh api"])
        Check.equal(picked, ["python scripts/categorize.py", "git log", "gh pr list"])

    def routine_writer(self) -> None:
        # Writes five files in dry-run mode with standing rules first, a byte copy of the runner, and nothing outside --dest.
        out = self._routine_out()
        dest = self.base / "routines" / "budget"
        before = self._snapshot(self.base, dest)
        with only_path(self.fakes.agent_dir):
            report = RoutineWriter(out, Redactor(Path("/home/me"))).write("idea-001", dest, "claude-code", "launchd")
        Check.equal(sorted(Path(f).name for f in report["files"]), sorted(["routine-prompt.md", "routine.json", "run_routine.py", "SCHEDULE.md", "com.vidbyte.routine.c1-budget-statements.plist"]))
        config = json.loads((dest / "routine.json").read_text(encoding="utf-8"))
        Check.equal((config["mode"], config["harness"], config["workdir"]), ("dry-run", "claude-code", str(self.base)))
        Check.true(Path(config["harness_path"]).name.lower().startswith("claude"), config["harness_path"])
        prompt = (dest / "routine-prompt.md").read_text(encoding="utf-8")
        Check.true(prompt.index("Standing rules") < prompt.index("## Routine") and "Read the newest statements file" in prompt, "prompt order")
        Check.equal((dest / "run_routine.py").read_bytes(), (SCRIPTS / "chores" / "run_routine.py").read_bytes(), "runner copy")
        Check.equal(self._snapshot(self.base, dest), before, "files outside --dest")
        Check.equal(report["registered"], False)
        Check.true("git push" not in config["allowed_commands"], "git push allowed")

    def routine_writer_refusals(self) -> None:
        # A non-empty --dest, an idea citing two chores, and a missing harness are usage errors.
        out = self._routine_out()
        writer = RoutineWriter(out, Redactor(Path("/home/me")))
        busy = self.base / "busy"
        Writer.text(busy / "keep.txt", "mine")
        for label, call in (
            ("non-empty dest", lambda: writer.write("idea-001", busy, "claude-code", "none")),
            ("two chores", lambda: writer.write("idea-002", self.base / "r2", "claude-code", "none")),
            ("bad time", lambda: writer.write("idea-001", self.base / "r3", "claude-code", "none", time="9am")),
        ):
            with only_path(self.fakes.agent_dir):
                try:
                    call()
                except RoutineError:
                    continue
            raise AssertionError("%s did not raise RoutineError" % label)
        with only_path(self.base / "empty-bin"):
            try:
                writer.write("idea-001", self.base / "r4", "codex", "none")
            except RoutineError as error:
                Check.true("not on PATH" in str(error), str(error))
            else:
                raise AssertionError("a missing harness did not raise")
        Check.equal((busy / "keep.txt").read_text(encoding="utf-8"), "mine", "the user's file")

    def _routine_out(self) -> Path:
        # A minimal out folder with chores.json and result.json for routine tests.
        out = self.base / "routine-out"
        if (out / "result.json").exists():
            return out
        chore = {"tag": "C1", "label": "Clean up my budget", "terms": ["budget", "statements"], "cwd": str(self.base),
                 "cadence": {"kind": "monthly", "pattern": "first_business_day", "label": "monthly, on the first business day", "time": "09:10", "anchor": "2026-06-01"},
                 "runs": [{"day": "2026-08-03", "sessions": ["S1"]}, {"day": "2026-09-01", "sessions": ["S2"]}], "prompts": ["P1"],
                 "inputs": {"files_read": [["statements/<date>.csv", 2]], "commands": [["python scripts/categorize.py", 2]], "credentials": [["STRIPE_API_KEY", 1]]},
                 "outputs": {"files_written": [["budget/<date>.xlsx", 2]], "external_actions": [{"action": "git push", "runs": 1, "tags": []}]},
                 "commands_for_routine": ["python scripts/categorize.py", "git push", "git log"]}
        other = dict(chore, tag="C2", label="Invoices", cadence={"kind": "weekly", "weekday": 0, "label": "weekly, on Mondays", "time": "08:30"}, runs=[{"day": "2026-09-07", "sessions": ["S3"]}])
        ideas = [
            {"id": "idea-001", "rank": 1, "title": "[C1] Budget: first-business-day routine", "summary": "s", "suggested_actions": ["ROUTINE PROMPT: Read the newest statements file and categorize it.", "SCHEDULE: x"], "completion_criteria": "c"},
            {"id": "idea-002", "rank": 2, "title": "[C1] and [C2]", "summary": "s", "suggested_actions": ["x"], "completion_criteria": "c"},
        ]
        Writer.text(out / "chores.json", json.dumps({"chores": [chore, other], "sessions": []}))
        Writer.text(out / "result.json", json.dumps({"kind": "suggestions.result", "data": {"ideas": ideas}}))
        return out

    @staticmethod
    def _snapshot(base: Path, skip: Path) -> list:
        # Every file under base outside skip, for the nothing-written-elsewhere check.
        return sorted(str(p) for p in base.rglob("*") if p.is_file() and skip not in p.parents and p != skip and "__pycache__" not in str(p) and "fake" not in p.name)

    def opencode_bad_db(self) -> None:
        # A non-SQLite opencode.db shows as a read[] error entry, and the other harnesses still run.
        data_dir = self.base / "oc-bad"
        Writer.text(data_dir / "opencode.db", "this is not a database" * 50)
        home = self.base / "oc-home"
        CodexFixture.session(home, "cx-1", str(home / "repo"), Dates.utc(TODAY - dt.timedelta(days=1)), ["Summarize the open issues in the tracker by label"], [])
        result = SessionCollector(CollectOptions(opencode_dir=str(data_dir), codex_home=str(home / ".codex"), disabled={"claude", "gemini", "qwen", "openclaude", "factory", "pi"}), home=home).collect()
        entry = next(e for e in result.read if e["harness"] == "opencode")
        Check.equal(entry["status"], "error", "status")
        Check.true(entry.get("error"), "no error text: %r" % entry)
        Check.true(any(s.harness == "codex" for s in result.sessions), "codex not read after the OpenCode failure")


class IntegrationTests:
    """Section 10 integration tests: subprocesses over the fixture home with the fakes."""

    def __init__(self, home: Home, fakes: Fakes, runner: Runner, base: Path) -> None:
        # Keeps the fixtures and the output folder shared by the flow.
        self.home = home
        self.fakes = fakes
        self.runner = runner
        self.base = base
        self.out = base / "out"
        self.gathered = None

    def gather_end_to_end(self) -> None:
        # Two chores with the right cadences and next dates; the irregular repeat and the scheduled job stay local.
        code, doc, out, err = self.runner.run("gather", "--out", str(self.out))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-800:])
        self.gathered = doc
        kinds = sorted(c["cadence"] for c in doc["chores"])
        Check.equal(kinds, ["monthly, on the first business day", "weekly, on Mondays"], "cadences")
        index = json.loads((self.out / "chores.json").read_text(encoding="utf-8"))
        monthly = next(c for c in index["chores"] if c["cadence"]["kind"] == "monthly")
        weekly = next(c for c in index["chores"] if c["cadence"]["kind"] == "weekly")
        last = self.home.monthly_days[-1]
        year, month = divmod(last.year * 12 + last.month, 12)
        Check.equal(monthly["cadence"]["next_expected"], Dates.first_business_day(year, month + 1).isoformat(), "monthly next")
        Check.equal(weekly["cadence"]["next_expected"], (self.home.weekly_days[-1] + dt.timedelta(days=7)).isoformat(), "weekly next")
        Check.equal(len(monthly["runs"]), 5, "the next-day session joined its run")
        Check.true(["statements/<date>.csv", 5] in monthly["inputs"]["files_read"], "inputs: %r" % monthly["inputs"])
        Check.true(any(e["action"] == "asked to send" for e in weekly["outputs"]["external_actions"]), "weekly outside action")
        digest = (self.out / "digest.md").read_text(encoding="utf-8")
        Check.true("dependency report" not in digest and "build queue" not in digest, "an irregular or automated repeat reached the digest")
        Check.true(any("dependency report" in i["label"] for i in index["irregular"]) or any("dependency" in i["label"] for i in doc["irregular"]), "irregular repeat not listed")
        Check.equal(len(doc["already_automated"]), 1, "already automated")
        Check.true(any(e["harness"] == "opencode" and e["kept"] == 1 for e in doc["read"]["read"]), "the OpenCode legacy session was not read")
        kept = doc["read"]["kept_by_harness"]
        Check.equal([kept.get(h) for h in ("openclaude", "factory-droid", "pi")], [1, 1, 1], "sessionkit v4 harnesses kept")

    def gather_only(self) -> None:
        # --only codex reads Codex alone (overriding --no-codex): only the weekly chore is found.
        code, doc, _, err = self.runner.run("gather", "--only", "codex", "--no-codex", "--out", str(self.base / "o-only"))
        Check.equal(code, 0, "exit (%s)" % err[-400:])
        Check.equal(sorted(doc["read"]["kept_by_harness"]), ["codex"], "harnesses read")
        Check.equal([c["cadence"] for c in doc["chores"]], ["weekly, on Mondays"], "chores")

    def gather_no_secrets(self) -> None:
        # No raw secret, email, or home path reaches digest.md, inputs.json, or chores.json texts.
        for name in ("digest.md", "inputs.json"):
            text = (self.out / name).read_text(encoding="utf-8")
            for needle in (SECRET_KEY, SECRET_EMAIL, str(self.home.path), str(self.home.path).replace("\\", "/")):
                Check.true(needle not in text, "%s leaked into %s" % (needle, name))
        prompts = json.dumps(json.loads((self.out / "chores.json").read_text(encoding="utf-8"))["prompts"])
        Check.true(SECRET_KEY not in prompts and SECRET_EMAIL not in prompts, "secret in chores.json prompts")

    def gather_credentials(self) -> None:
        # The credential is named (STRIPE_API_KEY, .env file) without its value.
        index = json.loads((self.out / "chores.json").read_text(encoding="utf-8"))
        monthly = next(c for c in index["chores"] if c["cadence"]["kind"] == "monthly")
        names = [n for n, _ in monthly["inputs"]["credentials"]]
        Check.true("STRIPE_API_KEY" in names and ".env file" in names, "credentials: %r" % names)

    def gather_bad_options(self) -> None:
        # --max-chars below 20,000 and --min-runs below 3 are usage errors.
        Check.equal(self.runner.run("gather", "--max-chars", "1000", "--out", str(self.base / "o1"))[0], 2, "max-chars")
        Check.equal(self.runner.run("gather", "--min-runs", "2", "--out", str(self.base / "o2"))[0], 2, "min-runs")

    def gather_nothing(self) -> None:
        # No sessions gives no_sessions; sessions without a chore give no_chores; both exit 3.
        code, doc, _, _ = self.runner.run("gather", "--no-claude", "--no-codex", "--no-opencode", "--no-gemini", "--no-qwen", "--no-openclaude", "--no-factory", "--no-pi", "--out", str(self.base / "o3"))
        Check.equal((code, doc and doc.get("reason")), (3, "no_sessions"))
        code, doc, _, _ = self.runner.run("gather", "--project-dir", str(self.home.path / "web0"), "--out", str(self.base / "o4"))
        Check.equal((code, doc and doc.get("reason")), (3, "no_chores"))

    def gather_bad_opencode_db(self) -> None:
        # A broken opencode.db shows as a read[] entry with status "error" in the gather summary; the rest still runs.
        bad = self.base / "bad-oc"
        Writer.text(bad / "opencode.db", "not sqlite " * 80)
        code, doc, _, err = self.runner.run("gather", "--opencode-dir", str(bad), "--out", str(self.base / "o5"))
        Check.equal(code, 0, "exit (%s)" % err[-400:])
        entry = next(e for e in doc["read"]["read"] if e["harness"] == "opencode")
        Check.equal(entry["status"], "error", "status")
        Check.true(entry.get("error") and str(self.base) not in entry["error"], "error text missing or leaks a path: %r" % entry)
        Check.equal(len(doc["chores"]), 2, "chores still found")

    def suggest_dry_run(self) -> None:
        # The recorded argv is exactly the expected one, run from the output folder with relative files; no paid-run.txt.
        code, doc, _, err = self.runner.run("suggest", "--out", str(self.out), "--dry-run", "--cli", self.fakes.cli)
        Check.equal(code, 0, "exit (%s)" % err[-400:])
        call = self.fakes.cli_calls()[-1]
        inputs = json.loads((self.out / "inputs.json").read_text(encoding="utf-8"))
        expected = ["--json", "--no-input", "agents", "suggest", "run", "--goal", inputs["goal"], "--category", "delegation", "--category", "leverage", "--category", "risk_prevention", "--count", "2", "--horizon", "any"]
        for kind, (flag, _) in FLAG_KINDS.items():
            for value in inputs.get(kind) or []:
                expected += [flag, value]
        expected += ["--files", "digest.md", "--instructions", "chore-routines.md", "--max-total-tokens", "400000", "--dry-run"]
        Check.equal(call["argv"], expected, "argv")
        Check.equal(Path(call["cwd"]).resolve(), self.out.resolve(), "cwd")
        Check.true(not (self.out / "paid-run.txt").exists(), "a dry run wrote paid-run.txt")
        Check.equal(doc["stop_reason"], "dry_run")

    def suggest_paid_once(self) -> None:
        # The paid run writes paid-run.txt and result.json; a second paid run is refused before any call.
        code, doc, _, _ = self.runner.run("suggest", "--out", str(self.out), "--paid", "--cli", self.fakes.cli)
        Check.equal(code, 0, "exit")
        Check.true((self.out / "paid-run.txt").exists() and (self.out / "result.json").exists(), "paid files")
        Check.equal(doc["estimated_cost_usd"], round((200000 * 2 + 100000 * 0.2 + 50000 * 10) / 1e6 * 1.02, 2), "cost")
        calls = len(self.fakes.cli_calls())
        Check.equal(self.runner.run("suggest", "--out", str(self.out), "--paid", "--cli", self.fakes.cli)[0], 2, "second paid run")
        Check.equal(len(self.fakes.cli_calls()), calls, "the fake was called again")

    def suggest_cli_error(self) -> None:
        # A CLI failure passes its exit code and error JSON through.
        folder = self._copy_out("err")
        code, doc, _, _ = self.runner.run("suggest", "--out", str(folder), "--paid", "--cli", self.fakes.cli, extra_env={"FAKE_CLI_EXIT": "5"})
        Check.equal((code, doc["error"]["code"]), (5, "insufficient_credits"))

    def suggest_no_cli(self) -> None:
        # Without --cli and with no vidbyte-cli on PATH: exit 127, nothing runs.
        folder = self._copy_out("nocli")
        calls = len(self.fakes.cli_calls())
        Check.equal(self.runner.run("suggest", "--out", str(folder), "--dry-run")[0], 127, "exit")
        Check.equal(len(self.fakes.cli_calls()), calls, "something ran")

    def suggest_bad_inputs(self) -> None:
        # An untagged risks value stops the run with exit 2 before any CLI call.
        folder = self._copy_out("badinputs")
        inputs = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
        inputs["risks"] = ["no tag here"]
        Writer.text(folder / "inputs.json", json.dumps(inputs))
        calls = len(self.fakes.cli_calls())
        Check.equal(self.runner.run("suggest", "--out", str(folder), "--dry-run", "--cli", self.fakes.cli)[0], 2, "exit")
        Check.equal(len(self.fakes.cli_calls()), calls, "the CLI ran with bad inputs")

    def suggest_project_flag(self) -> None:
        # --project is passed, right before --max-total-tokens, only when inputs.json sets it.
        folder = self._copy_out("project")
        inputs = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
        inputs["project"] = "automate-recurring-chores"
        Writer.text(folder / "inputs.json", json.dumps(inputs))
        Check.equal(self.runner.run("suggest", "--out", str(folder), "--dry-run", "--cli", self.fakes.cli)[0], 0, "exit")
        argv = self.fakes.cli_calls()[-1]["argv"]
        Check.equal(argv[-5:], ["--project", "automate-recurring-chores", "--max-total-tokens", "400000", "--dry-run"], "argv tail")

    def report_after_paid(self) -> None:
        # report.md maps ideas to chores with run dates and resume commands; flags appear.
        code, doc, _, err = self.runner.run("report", "--out", str(self.out))
        Check.equal(code, 0, "exit (%s)" % err[-400:])
        by_id = {i["id"]: i for i in doc["ideas"]}
        Check.true(by_id["idea-001"]["grounded"] and not by_id["idea-001"]["missing"], "idea-001: %r" % by_id["idea-001"])
        Check.true(any("cadence_mismatch" in f for f in by_id["idea-002"]["flags"]), "idea-002 flags: %r" % by_id["idea-002"]["flags"])
        Check.true(any("unguarded_external_action" in f for f in by_id["idea-002"]["flags"]), "unguarded")
        Check.equal(by_id["idea-003"]["grounded"], False, "idea-003")
        markdown = (self.out / "report.md").read_text(encoding="utf-8")
        Check.true("claude --resume budget-" in markdown and self.home.monthly_days[-1].isoformat() in markdown, "report.md lacks resume commands or run dates")
        Check.true("already automated" in markdown, "report.md does not list the automated repeat")
        for needle in (SECRET_KEY, SECRET_EMAIL, str(self.home.path)):
            Check.true(needle not in markdown, "%s leaked into report.md" % needle)

    def routine_and_runner(self) -> None:
        # routine writes the folder; the runner dry-runs the fake claude with the prompt on stdin, logs, and records the period.
        dest = self.base / "my-routines" / "budget"
        code, doc, _, err = self.runner.run("routine", "--out", str(self.out), "--idea", "idea-001", "--dest", str(dest), "--harness", "claude-code", "--scheduler", "cron")
        Check.equal(code, 0, "routine exit (%s)" % err[-600:])
        Check.true((dest / "crontab.txt").exists() and "--scheduled" in (dest / "crontab.txt").read_text(encoding="utf-8"), "crontab.txt")
        Check.true("1-5 * *" in doc["commands"]["cron_line"], doc["commands"]["cron_line"])
        before = len(self.fakes.agent_calls())
        code, _, _, err = self.runner.run(script=dest / "run_routine.py")
        Check.equal(code, 0, "dry run exit (%s)" % err[-600:])
        call = self.fakes.agent_calls()[-1]
        Check.equal(len(self.fakes.agent_calls()), before + 1, "calls")
        Check.equal(call["argv"], ["-p", CLAUDE_QUERY, "--output-format", "text", "--permission-mode", "plan"], "claude argv")
        Check.true(call["stdin"].startswith("Routine run on %s. Mode: DRY RUN." % TODAY.isoformat()) and "Standing rules" in call["stdin"], "stdin")
        Check.equal(Path(call["cwd"]).resolve(), Path(self.home.fin).resolve(), "cwd")
        Check.true(any(p.name.endswith("-dry-run.log") for p in (dest / "logs").iterdir()), "log")
        state = json.loads((dest / "state.json").read_text(encoding="utf-8"))
        Check.equal(len(state["dry_run_periods"]), 1, "period recorded")

    def runner_scheduled_guard(self) -> None:
        # --scheduled outside the window runs nothing; inside it runs once per period.
        dest = self.base / "my-routines" / "budget"
        window = RunWindow(json.loads((dest / "routine.json").read_text(encoding="utf-8"))["cadence"])
        outside = next(TODAY + dt.timedelta(days=k) for k in range(40, 80) if not window.in_window(TODAY + dt.timedelta(days=k)))
        inside = window.next_run(outside)
        before = len(self.fakes.agent_calls())
        code, doc, _, _ = self.runner.run("--scheduled", "--today", outside.isoformat(), script=dest / "run_routine.py")
        Check.equal((code, doc["run_day"]), (0, False), "outside")
        Check.equal(len(self.fakes.agent_calls()), before, "ran outside the window")
        Check.equal(self.runner.run("--scheduled", "--today", inside.isoformat(), script=dest / "run_routine.py")[0], 0, "inside")
        Check.equal(len(self.fakes.agent_calls()), before + 1, "did not run inside the window")
        code, doc, _, _ = self.runner.run("--scheduled", "--today", inside.isoformat(), script=dest / "run_routine.py")
        Check.true(code == 0 and "already ran" in doc["reason"], "second run: %r" % doc)
        Check.equal(len(self.fakes.agent_calls()), before + 1, "ran twice in one period")

    def runner_live_and_codex(self) -> None:
        # A live Claude run uses dontAsk with the allowed tools; a Codex routine uses exec with the read-only sandbox.
        dest = self.base / "my-routines" / "budget"
        self.runner.run("--live", script=dest / "run_routine.py")
        argv = self.fakes.agent_calls()[-1]["argv"]
        Check.equal(argv[4:6], ["--permission-mode", "dontAsk"], "live mode")
        Check.true(argv[6] == "--allowedTools" and "Bash(python scripts/categorize.py *)" in argv[7] and "git push" not in argv[7], "allowed tools: %r" % argv)
        Check.true(self.fakes.agent_calls()[-1]["stdin"].startswith("Routine run on %s. Mode: LIVE." % TODAY.isoformat()), "live header")
        codex_dest = self.base / "my-routines" / "budget-codex"
        code, _, _, err = self.runner.run("routine", "--out", str(self.out), "--idea", "idea-001", "--dest", str(codex_dest), "--harness", "codex", "--scheduler", "task-scheduler")
        Check.equal(code, 0, "codex routine (%s)" % err[-400:])
        self.runner.run(script=codex_dest / "run_routine.py")
        call = self.fakes.agent_calls()[-1]
        Check.equal(call["argv"], ["exec", "--skip-git-repo-check", "--cd", self.home.fin, "--sandbox", "read-only", "-"], "codex argv")
        Check.true("Register-ScheduledTask" in (codex_dest / "SCHEDULE.md").read_text(encoding="utf-8"), "SCHEDULE.md register command")

    def runner_failures(self) -> None:
        # A missing harness exits 127, a missing folder exits 2, a timeout exits 124; each writes a log.
        dest = self.base / "my-routines" / "budget"
        config_path = dest / "routine.json"
        original = config_path.read_text(encoding="utf-8")
        try:
            config = json.loads(original)
            Writer.text(config_path, json.dumps(dict(config, harness_path=str(self.base / "nope" / "claude.exe"))))
            Check.equal(self.runner.run(script=dest / "run_routine.py")[0], 127, "missing harness")
            Writer.text(config_path, json.dumps(dict(config, workdir=str(self.base / "gone"))))
            Check.equal(self.runner.run(script=dest / "run_routine.py")[0], 2, "missing folder")
            Writer.text(config_path, json.dumps(dict(config, timeout_minutes=0)))
            Check.equal(self.runner.run(script=dest / "run_routine.py", extra_env={"FAKE_AGENT_SLEEP": "5"})[0], 124, "timeout")
            Writer.text(config_path, "{not json")
            Check.equal(self.runner.run(script=dest / "run_routine.py")[0], 2, "bad routine.json")
        finally:
            Writer.text(config_path, original)

    def feedback_accept(self) -> None:
        # feedback sends the exact idea title and project; the reason is redacted.
        inputs_path = self.out / "inputs.json"
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        inputs["project"] = "automate-recurring-chores"
        Writer.text(inputs_path, json.dumps(inputs))
        code, _, _, _ = self.runner.run("feedback", "--out", str(self.out), "--idea", "idea-001", "--accept", "--reason", "mail %s works" % SECRET_EMAIL, "--cli", self.fakes.cli)
        Check.equal(code, 0, "exit")
        argv = self.fakes.cli_calls()[-1]["argv"]
        title = json.loads((self.out / "result.json").read_text(encoding="utf-8"))["data"]["ideas"][0]["title"]
        Check.equal(argv[:9], ["--json", "agents", "suggest", "feedback", "accept", "--project", "automate-recurring-chores", "--suggestion", title], "argv")
        Check.true(SECRET_EMAIL not in " ".join(argv) and "[email]" in argv[-1], "reason not redacted: %r" % argv[-1])

    def _copy_out(self, name: str) -> Path:
        # A fresh copy of the gathered folder without paid-run.txt or result.json.
        folder = self.base / ("out-%s" % name)
        if folder.exists():
            shutil.rmtree(str(folder))
        shutil.copytree(str(self.out), str(folder), ignore=shutil.ignore_patterns("paid-run.txt", "result.json*", "result.stderr.txt"))
        return folder


UNIT_CASES = [
    ("terms_periods", "Silent Failure"), ("terms_empty", "Edge Case"), ("terms_no_secret", "Silent Failure"), ("paths_normalized", "Silent Failure"),
    ("command_signatures", "Edge Case"), ("command_external", "Silent Failure"), ("action_parser", "Hidden Assumption"),
    ("signature_credentials", "Silent Failure"), ("signature_openers", "Edge Case"), ("template_prefix", "Silent Failure"), ("skill_body", "Hidden Assumption"),
    ("cadence_first_business_day", "Silent Failure"), ("cadence_weekly", "Edge Case"), ("cadence_weekdays", "Silent Failure"), ("cadence_irregular", "Silent Failure"),
    ("cadence_patterns", "Edge Case"), ("cadence_too_few", "Edge Case"), ("cadence_overdue", "Silent Failure"),
    ("window_first_business_day", "Hidden Failure"), ("window_clamp_biweekly", "Edge Case"), ("window_triggers", "Hidden Assumption"),
    ("detector_monthly_chore", "Silent Failure"), ("detector_same_day", "Hidden Failure"), ("detector_next_day", "Hidden Failure"), ("detector_no_chaining", "Silent Failure"),
    ("detector_empty", "Edge Case"), ("detector_external", "Silent Failure"), ("detector_template", "Silent Failure"), ("detector_automated", "Hidden Assumption"),
    ("detector_identical_short", "Edge Case"), ("digest_budget_and_tags", "Hidden Failure"), ("digest_focus_copy", "Edge Case"), ("digest_login_name", "Silent Failure"),
    ("suggest_count", "Edge Case"), ("validator_problems", "Edge Case"), ("reporter_flags", "Silent Failure"), ("reporter_ignores_evidence", "Hidden Assumption"),
    ("schedule_builder", "Silent Failure"), ("allowed_commands", "Silent Failure"), ("routine_writer", "Hidden Failure"), ("routine_writer_refusals", "Edge Case"),
    ("opencode_bad_db", "Hidden Failure"),
]
INTEGRATION_CASES = [
    ("gather_end_to_end", "Silent Failure"), ("gather_no_secrets", "Silent Failure"), ("gather_credentials", "Silent Failure"), ("gather_bad_options", "Edge Case"),
    ("gather_only", "Hidden Assumption"), ("gather_nothing", "Edge Case"), ("gather_bad_opencode_db", "Hidden Failure"), ("suggest_dry_run", "Silent Failure"), ("suggest_paid_once", "Hidden Failure"),
    ("suggest_cli_error", "Hidden Failure"), ("suggest_no_cli", "Hidden Assumption"), ("suggest_bad_inputs", "Edge Case"), ("suggest_project_flag", "Silent Failure"), ("report_after_paid", "Silent Failure"),
    ("routine_and_runner", "Hidden Failure"), ("runner_scheduled_guard", "Hidden Failure"), ("runner_live_and_codex", "Silent Failure"),
    ("runner_failures", "Edge Case"), ("feedback_accept", "Silent Failure"),
]


def main() -> int:
    # Builds the fixtures once, runs every case, and cleans up.
    base = Path(tempfile.mkdtemp(prefix="arc-test-"))
    results = Results()
    try:
        fakes = Fakes(base)
        home = Home(base)
        units = UnitTests(base / "unit", fakes)
        (base / "unit").mkdir(parents=True, exist_ok=True)
        for name, category in UNIT_CASES:
            results.run(name, category, getattr(units, name))
        integration = IntegrationTests(home, fakes, Runner(home, fakes, base), base)
        for name, category in INTEGRATION_CASES:
            results.run(name, category, getattr(integration, name))
    finally:
        shutil.rmtree(str(base), ignore_errors=True)
    return results.finish()


if __name__ == "__main__":
    sys.exit(main())
