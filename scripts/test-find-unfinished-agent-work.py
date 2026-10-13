#!/usr/bin/env python3
"""Verification script for skills/find-unfinished-agent-work (design doc section 10).

Unit tests import the skill's modules and build sessions in memory. Git tests create real
repositories (with a bare repository as the remote) in a temporary folder. Integration tests write
Claude Code and Codex stores into a temporary home and run unfinished_work.py in subprocesses whose
HOME, harness folders, and PATH all point into that home. Every suggest and feedback call goes to a
recording fake vidbyte-cli passed with --cli; PATH holds no vidbyte-cli, so an ignored --cli fails
with 127 instead of reaching a real (billing) CLI. A fake gh is built the same way. Run it with every
Python you support:

    python scripts/test-find-unfinished-agent-work.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "find-unfinished-agent-work"
SCRIPTS = SKILL / "scripts"
ENTRY = SCRIPTS / "unfinished_work.py"
FOCUS = SKILL / "references" / "unfinished-work-checklist.md"
sys.path.insert(0, str(SCRIPTS))

from sessionkit.collector import CollectResult  # noqa: E402
from sessionkit.model import Clock, Session  # noqa: E402
from unfinished.digest import DigestWriter  # noqa: E402
from unfinished.followup import LaterMatch, LaterWorkMatcher, ProjectKeys  # noqa: E402
from unfinished.repo_state import BranchState, FolderState, PullState, RepoStateProbe  # noqa: E402
from unfinished.report import ChecklistReporter  # noqa: E402
from unfinished.resume import ResumeCommandBuilder  # noqa: E402
from unfinished.signals import PastedText, TaskText, UnfinishedDetector  # noqa: E402
from unfinished.status import FOLDED, PROBABLY_DONE, STILL_OPEN, UNKNOWN, StatusJudge  # noqa: E402

SECRET_KEY = "sk-abcdefghijklmnopqrstuvwx1234"
SECRET_GH = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
SECRET_EMAIL = "bob.builder@example.com"
STRIP_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "VIDBYTE_API_KEY", "PYTHONPATH")
PASTE = "\n".join([
    "# speak-clear",
    "## Core principle",
    "The goal is efficient understanding, not simplification of the ideas themselves.",
    "A reader should be able to follow the reasoning in one pass, without re-reading.",
    "Clarity and precision are not in tension with depth or nuance.",
    "Prefer the concrete example over the abstract claim whenever both would work.",
])
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
            {"id": "idea-001", "rank": 1, "title": "Finish the webhook retry backoff and push the branch", "summary": "[S1] repo, last touched 2 days ago. Todos stayed open.", "first_action": "Resume [S1] and run the tests", "completion_criteria": "The branch is merged.", "horizon": "now", "readiness": "ready", "primary_category": "completion"},
            {"id": "idea-002", "rank": 2, "title": "Clean up everything", "summary": "General advice with no tags.", "horizon": "later", "primary_category": "completion"},
        ]
        usage = {"model_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0} if dry else {"model_calls": 5, "input_tokens": 300000, "cached_input_tokens": 100000, "output_tokens": 50000}
        data = {"status": "no_suggestions" if dry else "complete", "stop_reason": "dry_run" if dry else "completed", "settings": {"model": "gpt-6-sol", "critic_model": "gpt-6-sol"}, "usage": usage, "ideas": ideas, "context_manifest": [{"ref": "ctx-001", "kind": "file", "status": "included"}], "warnings": []}
        print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": data}))
        return 0
    print(json.dumps({"schema_version": 1, "kind": "ok", "data": {}}))
    return 0
'''
FAKE_GH_SOURCE = r'''
import json, os, sys

def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\n")
    if argv[:2] == ["auth", "status"]:
        return int(os.environ.get("FAKE_GH_AUTH", "0") or 0)
    if argv[:2] == ["pr", "view"]:
        number = int(argv[2].rstrip("/").rsplit("/", 1)[-1])
        states = json.loads(os.environ.get("FAKE_GH_STATES", "{}") or "{}")
        print(json.dumps({"number": number, "state": states.get(str(number), "OPEN"), "isDraft": False, "mergedAt": None}))
        return 0
    return 1
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
        for name in self.failed:
            print("  failed: %s" % name)
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
    def ts(minutes: float):
        # An aware UTC datetime `minutes` after the base.
        return Clock.parse_ts(Stamp.at(minutes))

    @staticmethod
    def iso(minutes: float) -> str:
        # An ISO 8601 UTC string with milliseconds and a Z, like Claude Code and Codex write.
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(Stamp.at(minutes))) + ".123Z"


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
        # Writes text with LF endings.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


class SessionBuilder:
    """Builds in-memory sessions for detector, matcher, status, and digest unit tests."""

    @staticmethod
    def make(prompts: list, harness: str = "claude-code", sid: str = "s1", cwd: str = "/code/repo", start: float = 0, end: str = "finished", final: str = "All done.", todos=None, actions: tuple = (), failed: int = 0, title: str = "", reply: bool = True, source: str = "mem") -> Session:
        # One session: prompts 10 minutes apart, then the last turn's actions, todos, reply, and ending.
        session = Session(harness, sid, source)
        session.add_cwd(cwd)
        session.title = title
        for index, text in enumerate(prompts):
            if index:
                session.note_agent_text("Done with step %d." % index, Stamp.ts(start + index * 10 - 5))
                session.note_turn_end("finished")
            session.add_turn(text, Stamp.ts(start + index * 10))
        for action in actions:
            session.note_action(action)
        for _ in range(failed):
            session.note_action("Bash: pytest -q", failed=True)
        if todos is not None:
            session.note_todos([{"content": c, "status": s} for c, s in todos])
        if reply:
            session.note_agent_text(final, Stamp.ts(start + len(prompts) * 10 - 2))
        if end == "interrupted":
            session.note_interrupt()
        elif end in ("finished", "error"):
            session.note_turn_end(end)
        return session.finish()


class Git:
    """Runs git for fixture repositories with an isolated identity and configuration."""

    def __init__(self, base: Path) -> None:
        # base/gitcfg holds an empty global config so the user's settings never apply to fixtures.
        cfg = Writer.text(base / "gitcfg" / "config", "")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@t.invalid",
                        GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@t.invalid", GIT_TERMINAL_PROMPT="0")

    def __call__(self, cwd: Path, *args: str) -> str:
        # Runs one git command and returns stdout; raises on failure.
        done = subprocess.run(["git", "-c", "init.defaultBranch=main"] + list(args), cwd=str(cwd), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if done.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr.decode("utf-8", "replace")))
        return done.stdout.decode("utf-8", "replace")

    def repo_with_remote(self, base: Path, name: str) -> Path:
        # A work repository with one pushed commit on main and a bare remote.
        remote = base / (name + "-remote.git")
        work = base / name
        remote.mkdir(parents=True)
        work.mkdir(parents=True)
        self(remote, "init", "--bare", "-q")
        self(work, "init", "-q")
        Writer.text(work / "app.py", "print('hi')\n")
        self(work, "add", "app.py")
        self(work, "commit", "-q", "-m", "first")
        self(work, "remote", "add", "origin", str(remote))
        self(work, "push", "-q", "-u", "origin", "main")
        return work

    def commit(self, work: Path, name: str, text: str) -> None:
        # Writes one file and commits it.
        Writer.text(work / name, text)
        self(work, "add", name)
        self(work, "commit", "-q", "-m", "change " + name)


class Launcher:
    """A real launcher (an .exe on Windows) for a fake tool, made with pip's vendored distlib."""

    @staticmethod
    def make(base: Path, module: str, source: str, command: str) -> tuple:
        # Writes <module>.py and builds the `command` launcher; returns (launcher path, module folder, bin folder).
        module_dir = base / (module + "-src")
        Writer.text(module_dir / (module + ".py"), source)
        bin_dir = base / (module + "-bin")
        bin_dir.mkdir(parents=True, exist_ok=True)
        from pip._vendor.distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, str(bin_dir))
        maker.executable = sys.executable
        maker.variants = {""}
        made = maker.make("%s = %s:main" % (command, module))
        return made[0], module_dir, bin_dir


class FakeProbe:
    """A RepoStateProbe stand-in that returns prepared folder, branch, and pull request states."""

    def __init__(self, folder_state=None, branches: dict = None, pulls: dict = None) -> None:
        # Holds the prepared answers.
        self.git = "git"
        self.folder_state = folder_state
        self.branches = branches or {}
        self.pulls = pulls or {}

    def folder(self, cwd: str):
        # The prepared folder state.
        return self.folder_state

    def branch(self, folder, name: str):
        # The prepared branch state, or an unknown one.
        return self.branches.get(name, BranchState(name))

    def pull(self, url: str, cwd: str = None):
        # The prepared pull request state, or None.
        return self.pulls.get(url)


class Pipeline:
    """Runs detector, matcher, and judge in memory, like gather does."""

    @staticmethod
    def items(sessions: list, probe=None, now=None) -> tuple:
        # (findings, matches, items) for the sessions.
        findings = UnfinishedDetector().detect(sessions)
        matches = LaterWorkMatcher(ProjectKeys(use_git=False)).match(findings)
        items = StatusJudge(probe, now=now).judge(findings, matches)
        return findings, matches, items

    @staticmethod
    def collect_result(sessions: list) -> CollectResult:
        # A CollectResult holding the sessions, as the collector would return it.
        result = CollectResult(None, Clock.now())
        result.sessions = list(sessions)
        result.read = [{"harness": "claude-code", "root": "/x", "status": "read", "candidates": len(sessions), "kept": len(sessions)}]
        return result


class UnitTests:
    """Section 10 unit tests (modules imported directly)."""

    def __init__(self, base: Path) -> None:
        # Keeps a scratch folder and builds the git helper.
        self.base = base
        self.git = Git(base)
        self.work = base / "work"
        self.work.mkdir(parents=True, exist_ok=True)

    # ---- detector -------------------------------------------------------------------------------

    def detector_mid_turn(self) -> None:
        # end_state "working" on a harness that records turn ends gives stopped_mid_turn.
        done = SessionBuilder.make(["Write the parser for the config files"], sid="a")
        working = SessionBuilder.make(["Port the importer to the new storage layer"], sid="b", end="", reply=False, actions=("Edit src/importer.py",))
        findings = UnfinishedDetector().detect([done, working])
        Check.equal(working.end_state, "working", "fixture end state")
        Check.true("stopped_mid_turn" in findings[1].signal_ids(), "no stopped_mid_turn: %r" % findings[1].signal_ids())
        Check.true(findings[1].candidate, "mid-turn session is not a candidate")

    def detector_no_turn_ends(self) -> None:
        # A harness whose sessions never record a turn end does not get stopped_mid_turn.
        working = SessionBuilder.make(["Port the importer to the new storage layer"], harness="gemini-cli", end="", reply=False)
        finding = UnfinishedDetector().detect([working])[0]
        Check.true("stopped_mid_turn" not in finding.signal_ids(), "stopped_mid_turn on a harness without turn ends")

    def detector_endings(self) -> None:
        # interrupted, error, and a decision question are candidates; an offer of extra work alone is not.
        interrupted = SessionBuilder.make(["Rename the billing tables and update the queries"], end="interrupted")
        error = SessionBuilder.make(["Rename the billing tables and update the queries"], end="error")
        question = SessionBuilder.make(["Rename the billing tables and update the queries"], final="Should I drop the legacy column now or after the backfill?")
        offer = SessionBuilder.make(["Rename the billing tables and update the queries"], final="Renamed them. Want me to also add docs?")
        found = UnfinishedDetector().detect([interrupted, error, question, offer])
        Check.true("interrupted" in found[0].signal_ids() and found[0].candidate, "interrupted: %r" % found[0].signal_ids())
        Check.true("ended_in_error" in found[1].signal_ids() and found[1].candidate, "error: %r" % found[1].signal_ids())
        Check.true("unanswered_question" in found[2].signal_ids() and found[2].candidate, "question: %r" % found[2].signal_ids())
        Check.equal(found[3].signal_ids(), ["offered_follow_up"], "offer signals")
        Check.true(not found[3].candidate, "an offer alone made a candidate")

    def detector_todos(self) -> None:
        # Todos [completed, in_progress, pending] give open_todos with 2 open; completed or cancelled give none.
        mixed = SessionBuilder.make(["Ship the export feature"], todos=[("Write code", "completed"), ("Write tests", "in_progress"), ("Ship", "pending")])
        closed = SessionBuilder.make(["Ship the export feature"], todos=[("Write code", "completed"), ("Ship", "cancelled")])
        found = UnfinishedDetector().detect([mixed, closed])
        Check.true("open_todos" in found[0].signal_ids(), "no open_todos")
        Check.equal(len(found[0].open_todos), 2, "open todo count")
        Check.true("open_todos" not in found[1].signal_ids(), "open_todos for closed todos")

    def detector_remaining(self) -> None:
        # "Next steps: wire the webhook." is remaining_work; "Nothing remaining; all tests pass." is not.
        left = SessionBuilder.make(["Build the webhook handler"], final="Handler added. Next steps: wire the webhook into the router.")
        done = SessionBuilder.make(["Build the webhook handler"], final="Nothing remaining; all tests pass.")
        todo_list = SessionBuilder.make(["Build the webhook handler"], final="I updated the todo list and finished every item.")
        found = UnfinishedDetector().detect([left, done, todo_list])
        Check.true("remaining_work" in found[0].signal_ids(), "no remaining_work")
        Check.true("remaining_work" not in found[1].signal_ids(), "remaining_work on a negation")
        Check.true("remaining_work" not in found[2].signal_ids(), "remaining_work on 'todo list'")

    def detector_no_reply(self) -> None:
        # A last turn with no reply and no interrupt gives no_reply.
        session = SessionBuilder.make(["Build the webhook handler", "now add retries to it please"], reply=False, end="")
        finding = UnfinishedDetector().detect([session])[0]
        Check.true("no_reply" in finding.signal_ids(), "no no_reply: %r" % finding.signal_ids())

    def detector_failed_weights(self) -> None:
        # 1 failed action in a finished last turn weighs 1 and is not a candidate alone; 3 failed weigh 2.
        one = SessionBuilder.make(["Build the webhook handler"], failed=1)
        three = SessionBuilder.make(["Build the webhook handler"], failed=3)
        found = UnfinishedDetector().detect([one, three])
        weights = {s.id: s.weight for s in found[0].signals}
        Check.equal(weights.get("failed_at_end"), 1, "one failure weight")
        Check.true(not found[0].candidate, "one failure alone made a candidate")
        Check.equal({s.id: s.weight for s in found[1].signals}.get("failed_at_end"), 2, "three failures weight")

    def detector_trivial(self) -> None:
        # "hi" is never a candidate; zero turns give no signals; "hello" twice with no tool call is trivial.
        hi = SessionBuilder.make(["hi"], end="interrupted")
        empty = Session("claude-code", "e", "mem").finish()
        hello = SessionBuilder.make(["hello", "hello"], end="", reply=False)
        found = UnfinishedDetector().detect([hi, empty, hello])
        Check.equal([f.signal_ids() for f in found], [[], [], []], "trivial signals")

    def detector_branches_and_prs(self) -> None:
        # Branches from checkout -b, switch -c, worktree add -b, and push -u are deduped; PR URLs split into own and mentioned.
        actions = ("Bash: git checkout -b feat/x", "Bash: git switch -c fix-y", "Bash: git worktree add ../w -b feat/z", "Bash: git push -u origin feat/x", "Bash: git checkout -b main", "Bash: gh pr create --fill")
        own = "https://github.com/acme/app/pull/12"
        mentioned = "https://github.com/acme/app/pull/7"
        session = SessionBuilder.make(["Add retries to the webhook client"], actions=actions, final="Opened %s, like the earlier %s." % (own, mentioned))
        finding = UnfinishedDetector().detect([session])[0]
        Check.equal(finding.branches, ["feat/x", "fix-y", "feat/z"], "branches")
        Check.equal(finding.pr_urls, [own, mentioned], "a URL printed in the turn that ran gh pr create is the session's own")
        other = SessionBuilder.make(["Explain how the webhook client retries"], final="It works like %s." % mentioned)
        other_finding = UnfinishedDetector().detect([other])[0]
        Check.equal(other_finding.pr_urls, [], "a merely mentioned PR became decisive")
        Check.equal(other_finding.mentioned_pr_urls, [mentioned], "mentioned PRs")
        task = SessionBuilder.make(["Resolve the review comments on PR #7"], final="Replied on %s." % mentioned)
        Check.equal(UnfinishedDetector().detect([task])[0].pr_urls, [mentioned], "a PR the request names is the session's own")

    def detector_keywords(self) -> None:
        # Stopwords and agent-chat words are not keywords; #123 and retry.py are.
        words = TaskText.keywords("Please fix the bug in retry.py for issue #123 and run the tests, thanks claude")
        Check.true("#123" in words and "retry.py" in words, "missing keywords: %r" % words)
        Check.true(not ({"please", "fix", "the", "bug", "run", "tests", "thanks", "claude"} & words), "stopwords kept: %r" % words)

    def detector_numbers(self) -> None:
        # PR and issue numbers are read from "#41", "PR 85", and ".../pull/9"; CSS colors like #333 are not.
        Check.equal(TaskText.numbers("Resolve PR #41", "see PR 85 and https://github.com/a/b/pull/9", "color: #333; border: #000000"), {"41", "85", "9"}, "numbers")

    def detector_pasted_text(self) -> None:
        # A skill pasted into 3 sessions is stripped from the request; a one-line request retried 3 times is kept.
        sessions = [SessionBuilder.make([PASTE + "\n" + request], sid="p%d" % i, start=i * 100) for i, request in enumerate(["Explain PR 502", "Investigate the flaky login test", "Draft the release notes"])]
        sessions += [SessionBuilder.make(["Resolve PR #499 comments"], sid="r%d" % i, start=400 + i * 100) for i in range(3)]
        findings = UnfinishedDetector().detect(sessions)
        Check.equal(findings[0].first_request, "Explain PR 502", "stripped request")
        Check.equal(findings[3].first_request, "Resolve PR #499 comments", "retried request")
        Check.equal(PastedText([]).remove_from("one\ntwo"), "one\ntwo", "nothing learned strips nothing")

    # ---- matcher --------------------------------------------------------------------------------

    def _background(self) -> list:
        # Four unrelated finished sessions in another folder, so word rarity means something.
        topics = ["Tune the Postgres autovacuum settings", "Write the onboarding email copy", "Profile the image resizer memory", "Draft the quarterly roadmap slides"]
        return [SessionBuilder.make([t], sid="bg%d" % i, cwd="/code/elsewhere", start=-500 + i * 10) for i, t in enumerate(topics)]

    def _pair(self, later_cwd: str = "/code/repo", later_start: float = 100, later_end: str = "finished", earlier_cwd: str = "/code/repo") -> tuple:
        # An unfinished earlier session and a later session of the same task.
        early = SessionBuilder.make(["Add exponential backoff to the webhook retry loop in billing worker"], sid="early", cwd=earlier_cwd, end="interrupted", actions=("Edit src/retry.py",))
        late = SessionBuilder.make(["Finish the exponential backoff for the webhook retry loop in billing worker and run tests"], sid="late", cwd=later_cwd, start=later_start, end=later_end, actions=("Edit src/retry.py",))
        return early, late

    def _match(self, sessions: list) -> tuple:
        # (findings by session id, matches) for the sessions.
        findings = UnfinishedDetector().detect(sessions)
        matches = LaterWorkMatcher(ProjectKeys(use_git=False)).match(findings)
        return {f.session.session_id: f for f in findings}, matches

    def matcher_same_task(self) -> None:
        # A later finished session in the same folder sharing distinctive words and a file marks the candidate finished later.
        early, late = self._pair()
        by_id, matches = self._match(self._background() + [early, late])
        match = matches.get(id(by_id["early"]))
        Check.true(match is not None, "no later match")
        Check.true(match.finished and match.finding is by_id["late"], "wrong match")
        Check.true("retry.py" in match.shared_files, "shared file missing: %r" % match.shared_files)

    def matcher_direction_and_project(self) -> None:
        # An earlier session never counts as later; a later session in another project never matches.
        early, late = self._pair(later_start=-100)
        by_id, matches = self._match(self._background() + [early, late])
        Check.true(id(by_id["early"]) not in matches, "an earlier session matched as later")
        early, late = self._pair(later_cwd="/code/other")
        by_id, matches = self._match(self._background() + [early, late])
        Check.true(id(by_id["early"]) not in matches, "a session in another project matched")

    def matcher_folders(self) -> None:
        # /code/repo does not match /code/repo2; C:\Code\Repo matches c:/code/repo/.
        early, late = self._pair(later_cwd="/code/repo2")
        by_id, matches = self._match(self._background() + [early, late])
        Check.true(id(by_id["early"]) not in matches, "/code/repo matched /code/repo2")
        early, late = self._pair(earlier_cwd="C:\\Code\\Repo", later_cwd="c:/code/repo/")
        by_id, matches = self._match(self._background() + [early, late])
        Check.true(id(by_id["early"]) in matches, "C:\\Code\\Repo did not match c:/code/repo/")

    def matcher_same_opening(self) -> None:
        # An identical first request (a resumed session) matches even with few keywords.
        early = SessionBuilder.make(["Make the login page work"], sid="early", end="interrupted")
        late = SessionBuilder.make(["Make the login page work"], sid="late", start=100)
        by_id, matches = self._match([early, late])
        match = matches.get(id(by_id["early"]))
        Check.true(match is not None and match.kind == "same_opening", "no same_opening match")

    def matcher_prefers_finished(self) -> None:
        # With both a finished and an unfinished later match, the finished one wins.
        early = SessionBuilder.make(["Make the login page work"], sid="early", end="interrupted")
        unfinished = SessionBuilder.make(["Make the login page work"], sid="mid", start=100, end="interrupted")
        finished = SessionBuilder.make(["Make the login page work"], sid="done", start=50)
        by_id, matches = self._match([early, unfinished, finished])
        match = matches.get(id(by_id["early"]))
        Check.true(match is not None and match.finished and match.finding is by_id["done"], "finished match did not win")

    def matcher_number_conflict(self) -> None:
        # Two sessions about different pull requests never match, however many words they share.
        early = SessionBuilder.make(["Resolve the review comments on PR #41 in vidbyte-cli"], sid="early", end="interrupted")
        late = SessionBuilder.make(["Resolve the review comments on PR #85 in vidbyte-cli"], sid="late", start=100)
        by_id, matches = self._match(self._background() + [early, late])
        Check.true(id(by_id["early"]) not in matches, "PR #41 matched PR #85")

    def matcher_pasted_opening(self) -> None:
        # The same pasted skill in front of different requests is not a "same first request" match.
        sessions = [SessionBuilder.make([PASTE + "\n" + r], sid="p%d" % i, start=i * 100, end="interrupted" if i == 0 else "finished") for i, r in enumerate(["Explain PR 502", "Investigate the flaky login test", "Draft the release notes"])]
        by_id, matches = self._match(sessions)
        Check.true(id(by_id["p0"]) not in matches, "a shared paste matched different tasks: %r" % (matches.get(id(by_id["p0"])) and matches[id(by_id["p0"])].evidence()))

    # ---- status ---------------------------------------------------------------------------------

    def _candidates(self, count: int, cwd: str) -> list:
        # count unfinished findings in cwd, 10 minutes apart.
        sessions = [SessionBuilder.make(["Task number %d about the exporter" % i], sid="c%d" % i, cwd=cwd, start=i * 10, end="interrupted") for i in range(count)]
        return UnfinishedDetector().detect(sessions)

    def status_chain(self) -> None:
        # C1 -> C2 -> C3 gives one item with two folded; a cycle neither loops nor folds everything away.
        f = self._candidates(3, str(self.work))
        matches = {id(f[0]): LaterMatch(f[1], "same_task", [], [], False), id(f[1]): LaterMatch(f[2], "same_task", [], [], False)}
        items = StatusJudge(None).judge(f, matches)
        visible = [i for i in items if i.status != FOLDED]
        Check.equal(len(visible), 1, "visible items")
        Check.equal(visible[0].finding, f[2], "head")
        Check.equal(len(visible[0].folded), 2, "folded count")
        g = self._candidates(2, str(self.work))
        cycle = {id(g[0]): LaterMatch(g[1], "same_task", [], [], False), id(g[1]): LaterMatch(g[0], "same_task", [], [], False)}
        items = StatusJudge(None).judge(g, cycle)
        Check.true(any(i.status != FOLDED for i in items), "a cycle folded every item away")

    def status_pull_requests(self) -> None:
        # Merged PR -> probably done; open PR -> still open; closed PR -> unknown.
        url = "https://github.com/acme/app/pull/12"
        for state, expected in (("MERGED", PROBABLY_DONE), ("OPEN", STILL_OPEN), ("CLOSED", UNKNOWN)):
            finding = self._candidates(1, str(self.work))[0]
            finding.pr_urls = [url]
            pull = PullState(url)
            pull.number, pull.state = 12, state
            item = StatusJudge(FakeProbe(pulls={url: pull})).judge([finding], {})[0]
            Check.equal(item.status, expected, state)
            Check.true("#12" in item.reason, "reason does not name the PR: %r" % item.reason)

    def status_later_but_unpushed(self) -> None:
        # Finished later but the session's branch has unpushed commits -> unknown, naming both facts.
        early, late = UnfinishedDetector().detect([
            SessionBuilder.make(["Add the retry backoff"], sid="e", cwd=str(self.work), end="interrupted", actions=("Bash: git checkout -b feat/x",)),
            SessionBuilder.make(["Add the retry backoff"], sid="l", cwd=str(self.work), start=100),
        ])
        folder = FolderState(str(self.work))
        folder.exists, folder.is_repo, folder.has_remote = True, True, True
        branch = BranchState("feat/x")
        branch.exists, branch.unpushed = True, 2
        matches = {id(early): LaterMatch(late, "same_opening", [], [], True)}
        item = [i for i in StatusJudge(FakeProbe(folder_state=folder, branches={"feat/x": branch})).judge([early, late], matches) if i.finding is early][0]
        Check.equal(item.status, UNKNOWN, "status")
        Check.true("later session" in item.reason and "on no remote" in item.reason, "reason: %r" % item.reason)

    def status_folder_and_running(self) -> None:
        # A missing folder -> unknown; active 5 minutes ago and working -> unknown "may still be running".
        gone = self._candidates(1, str(self.base / "no-such-folder"))[0]
        Check.equal(StatusJudge(None).judge([gone], {})[0].status, UNKNOWN, "missing folder")
        session = SessionBuilder.make(["Port the importer to the new storage layer"], cwd=str(self.work), end="", reply=False, actions=("Edit src/a.py",))
        done = SessionBuilder.make(["Write the parser for the config files"], sid="d", cwd=str(self.work))
        findings = UnfinishedDetector().detect([session, done])
        now = session.last_ts + dt.timedelta(minutes=5)
        item = [i for i in StatusJudge(None, now=now).judge(findings, {}) if i.finding is findings[0]][0]
        Check.equal(item.status, UNKNOWN, "running status")
        Check.true("may still be running" in item.reason, "reason: %r" % item.reason)

    def status_no_probe(self) -> None:
        # With no probe, a candidate is still open and its reason says git was not checked.
        item = StatusJudge(None).judge(self._candidates(1, str(self.work)), {})[0]
        Check.equal(item.status, STILL_OPEN, "status")
        Check.true("git not checked" in item.reason, "reason: %r" % item.reason)

    # ---- repository probe (real git) -----------------------------------------------------------

    def probe_folder_states(self) -> None:
        # Clean -> dirty 0; untracked + modified -> 2; a branch with no upstream; after a push and one commit, ahead 1.
        work = self.git.repo_with_remote(self.base / "p1", "repo")
        Check.equal(RepoStateProbe(enabled_gh=False).folder(str(work)).dirty, 0, "clean dirty")
        Writer.text(work / "new.txt", "x")
        Writer.text(work / "app.py", "print('changed')\n")
        Check.equal(RepoStateProbe(enabled_gh=False).folder(str(work)).dirty, 2, "dirty count")
        self.git(work, "checkout", "-q", "-b", "feat/local")
        state = RepoStateProbe(enabled_gh=False).folder(str(work))
        Check.true(state.has_upstream is False and state.has_remote, "upstream facts: %r" % state.to_json())
        self.git(work, "checkout", "-q", "main")
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "local")
        state = RepoStateProbe(enabled_gh=False).folder(str(work))
        Check.equal(state.ahead, 1, "ahead")
        Check.equal(state.branch, "main", "branch")

    def probe_branches(self) -> None:
        # A created branch with 2 commits on no remote reports unpushed 2; a deleted branch exists False.
        work = self.git.repo_with_remote(self.base / "p2", "repo")
        self.git(work, "checkout", "-q", "-b", "feat/two")
        self.git.commit(work, "a.txt", "a")
        self.git.commit(work, "b.txt", "b")
        self.git(work, "checkout", "-q", "main")
        self.git(work, "branch", "-q", "feat/gone")
        self.git(work, "branch", "-q", "-D", "feat/gone")
        probe = RepoStateProbe(enabled_gh=False)
        folder = probe.folder(str(work))
        two = probe.branch(folder, "feat/two")
        Check.true(two.exists and two.unpushed == 2, "feat/two: %r" % two.to_json())
        Check.equal(probe.branch(folder, "feat/gone").exists, False, "deleted branch")

    def probe_invalid_branch(self) -> None:
        # A branch name starting with "-" or containing ".." is refused without running git.
        probe = RepoStateProbe(enabled_gh=False, git_exe=str(self.base / "no-git-here.exe"))
        folder = FolderState(str(self.work))
        folder.exists, folder.is_repo = True, True
        for name in ("-rf", "a..b", "feat;rm"):
            Check.equal(probe.branch(folder, name).error, "invalid branch name", name)

    def probe_non_repo_and_cap(self) -> None:
        # A non-repo folder and a missing folder are reported, not raised; max_folders=1 checks only the first.
        plain = self.base / "plain"
        plain.mkdir()
        state = RepoStateProbe(enabled_gh=False).folder(str(plain))
        Check.true(state.exists and not state.is_repo, "non-repo: %r" % state.to_json())
        missing = RepoStateProbe(enabled_gh=False).folder(str(self.base / "missing"))
        Check.true(not missing.exists, "missing folder exists")
        capped = RepoStateProbe(enabled_gh=False, max_folders=1)
        Check.true(capped.folder(str(plain)) is not None, "first folder skipped")
        Check.true(capped.folder(str(self.work)) is None, "cap not applied")
        Check.equal(capped.summary()["folders_checked"], 1, "folders_checked")

    def probe_read_only(self) -> None:
        # Probing a repository whose files changed on disk leaves the index untouched and no lock behind.
        work = self.git.repo_with_remote(self.base / "p3", "repo")
        index = work / ".git" / "index"
        later = time.time() + 120
        os.utime(str(work / "app.py"), (later, later))
        before = index.stat().st_mtime_ns
        state = RepoStateProbe(enabled_gh=False).folder(str(work))
        Check.equal(state.dirty, 0, "touched-only file counted as dirty")
        Check.equal(index.stat().st_mtime_ns, before, "the index was rewritten")
        Check.true(not (work / ".git" / "index.lock").exists(), "index.lock left behind")

    def probe_gh(self) -> None:
        # gh logged out -> no pr view and "not logged in"; no gh -> "not installed"; logged in -> the PR state.
        gh, module_dir, _bin = Launcher.make(self.base, "fakegh", FAKE_GH_SOURCE, "gh")
        log = self.base / "fakegh-log.jsonl"
        url = "https://github.com/acme/app/pull/12"
        saved = {k: os.environ.get(k) for k in ("FAKE_GH_LOG", "FAKE_GH_AUTH", "FAKE_GH_STATES", "PYTHONPATH", "PATH")}
        try:
            os.environ.update(FAKE_GH_LOG=str(log), FAKE_GH_AUTH="1", PYTHONPATH=str(module_dir), FAKE_GH_STATES=json.dumps({"12": "MERGED"}))
            probe = RepoStateProbe(enabled_git=False, gh_exe=gh)
            Check.true(probe.pull(url, None) is None, "pull answered while logged out")
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
            Check.equal(calls, [["auth", "status"]], "gh calls while logged out")
            Check.equal(probe.summary()["gh"], "not logged in", "gh status")
            os.environ["FAKE_GH_AUTH"] = "0"
            ready = RepoStateProbe(enabled_git=False, gh_exe=gh)
            pull = ready.pull(url, None)
            Check.true(pull is not None and pull.state == "MERGED" and pull.number == 12, "pull: %r" % (pull and pull.to_json()))
            Check.equal(ready.summary()["gh"], "used", "gh status when logged in")
            os.environ["PATH"] = ""
            Check.equal(RepoStateProbe(enabled_git=False).summary()["gh"], "not installed", "gh missing")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # ---- resume ---------------------------------------------------------------------------------

    def resume_commands(self) -> None:
        # Each harness gives its command; a non-default Claude folder adds CLAUDE_CONFIG_DIR; PowerShell uses $env:; no folder gives a note.
        home = self.base / "rhome"
        builder = ResumeCommandBuilder(home)
        cwd = str(self.work)
        cases = {
            "claude-code": (str(home / ".claude-work" / "projects" / "x" / "s.jsonl"), "claude --resume sid", {"CLAUDE_CONFIG_DIR": str(home / ".claude-work")}),
            "codex": (str(home / ".codex" / "sessions" / "2026" / "r.jsonl"), "codex resume sid", {}),
            "opencode": (str(home / ".local" / "share" / "opencode" / "opencode.db") + "#sid", "opencode --session sid", {}),
            "gemini-cli": (str(home / ".gemini" / "tmp" / "h" / "chats" / "c.json"), "gemini --resume sid", {}),
            "qwen-code": (str(home / ".qwen" / "tmp" / "h" / "chats" / "c.json"), "qwen --resume sid", {}),
            "factory-droid": (str(home / ".factory" / "sessions" / "p" / "sid.jsonl"), "droid --resume sid", {}),
            "pi": (str(home / ".pi" / "agent" / "sessions" / "p" / "sid.jsonl"), "pi --session sid", {}),
        }
        for harness, (source, command, env) in cases.items():
            session = SessionBuilder.make(["Do the thing in the repo"], harness=harness, sid="sid", cwd=cwd, source=source)
            entry = builder.build(session)
            Check.equal(entry["command"], command, harness)
            Check.equal(entry["env"], env, harness + " env")
            Check.true(entry["bash"].startswith('cd "%s" && ' % cwd), harness + " bash: " + entry["bash"])
        claude = builder.build(SessionBuilder.make(["x y z w"], sid="sid", cwd=cwd, source=cases["claude-code"][0]))
        Check.true('$env:CLAUDE_CONFIG_DIR = "' in claude["powershell"] and claude["powershell"].startswith("Set-Location"), "powershell: " + claude["powershell"])
        default = builder.build(SessionBuilder.make(["x y z w"], sid="sid", cwd=cwd, source=str(home / ".claude" / "projects" / "x" / "s.jsonl")))
        Check.equal(default["env"], {}, "default Claude folder env")
        fork = builder.build(SessionBuilder.make(["x y z w"], harness="openclaude", sid="sid", cwd=cwd, source=str(home / ".openclaude" / "projects" / "x" / "s.jsonl")))
        Check.true(fork["command"] == "" and fork["bash"] == "" and "own session list" in fork["note"], "openclaude: %r" % fork)
        nowhere = builder.build(SessionBuilder.make(["x y z w"], sid="sid", cwd="gemini project ab12cd34", harness="gemini-cli"))
        Check.true(nowhere["cwd"] == "" and "not known" in nowhere["note"], "note: %r" % nowhere)

    # ---- digest ---------------------------------------------------------------------------------

    def _digest_sessions(self, home: Path, count: int = 6) -> list:
        # Open sessions in a real folder, one unknown (missing folder), one finished, all carrying secrets in their text.
        sessions = []
        for i in range(count):
            final = "Next steps: finish part %d. Key %s, mail %s, folder %s" % (i, SECRET_KEY, SECRET_EMAIL, str(home / "secret-project"))
            word = "zq%sx" % "".join(chr(97 + int(d)) for d in str(i))  # a distinct task word per session, so no two fold
            sessions.append(SessionBuilder.make(["Build the %s widget for the exporter with token %s" % (word, SECRET_GH)], sid="sess-secret-id-%06d" % i, cwd=str(self.work), start=i * 20, final=final, end="interrupted", todos=[("Write tests for the %s widget" % word, "pending")]))
        sessions.append(SessionBuilder.make(["Investigate the dropped queue messages"], sid="sess-gone-000001", cwd=str(home / "deleted-worktree"), start=500, end="interrupted"))
        sessions.append(SessionBuilder.make(["Explain how the cache works"], sid="sess-done-000001", cwd=str(self.work), start=600))
        return sessions

    def _write_digest(self, name: str, sessions: list, max_chars: int = 70000, home: Path = None):
        # Runs the pipeline and the digest writer into base/name; returns (out_dir, summary, writer, items).
        home = home or (self.base / "dhome")
        out = self.base / name
        out.mkdir(parents=True, exist_ok=True)
        _f, _m, items = Pipeline.items(sessions)
        writer = DigestWriter(out, home, max_chars, FOCUS)
        summary = writer.write(items, Pipeline.collect_result(sessions), {"git": "disabled", "gh": "disabled", "folders_checked": 0, "folder_cap": 80, "pull_requests_checked": 0})
        return out, summary, writer, items

    def digest_budget_and_order(self) -> None:
        # The digest never exceeds max_chars, and still-open blocks come before unknown blocks.
        home = self.base / "dhome"
        out, summary, _w, _i = self._write_digest("d1", self._digest_sessions(home, 6))
        text = (out / "digest.md").read_text(encoding="utf-8")
        Check.true(len(text) <= 70000, "digest too long")
        Check.true(text.index("## Still open") < text.index("## Status unknown"), "unknown before still open")
        big, summary, _w, _i = self._write_digest("d1b", self._digest_sessions(home, 120), max_chars=20000)
        Check.true(len((big / "digest.md").read_text(encoding="utf-8")) <= 20000, "digest over a 20,000 budget")
        Check.true(summary["in_digest"]["not_shown"] > 0, "a tiny budget showed everything, so the cap was not exercised")

    def digest_redaction(self) -> None:
        # No raw secret, email, home path, session id, or resume command appears in digest.md or inputs.json.
        home = self.base / "dhome"
        out, _s, _w, _i = self._write_digest("d2", self._digest_sessions(home, 6), home=home)
        for name in ("digest.md", "inputs.json"):
            text = (out / name).read_text(encoding="utf-8")
            for needle in (SECRET_KEY, SECRET_GH, SECRET_EMAIL, str(home), "sess-secret-id", "--resume", "codex resume"):
                Check.true(needle not in text, "%s leaks %r" % (name, needle))

    def digest_tags(self) -> None:
        # Every [S#]/[P#] in digest.md and inputs.json exists in sessions.json, and tags are unique.
        out, _s, _w, _i = self._write_digest("d3", self._digest_sessions(self.base / "dhome", 6))
        index = json.loads((out / "sessions.json").read_text(encoding="utf-8"))
        known = [e["tag"] for e in index["items"] if e["tag"]] + [p["tag"] for p in index["prompts"]]
        Check.equal(len(known), len(set(known)), "duplicate tags")
        cited = set()
        for name in ("digest.md", "inputs.json"):
            for group in re.findall(r"\[((?:[SP]\d+)(?:[ ,]+[SP]\d+)*)\]", (out / name).read_text(encoding="utf-8")):
                cited.update(re.findall(r"[SP]\d+", group))
        Check.true(cited and cited <= set(known), "unknown tags: %r" % (cited - set(known)))

    def digest_inputs(self) -> None:
        # Every tagged inputs.json value starts with a tag and is at most 500 characters; the goal is at most 700.
        out, _s, _w, _i = self._write_digest("d4", self._digest_sessions(self.base / "dhome", 6))
        data = json.loads((out / "inputs.json").read_text(encoding="utf-8"))
        Check.true(len(data["goal"]) <= 700, "goal too long")
        for key in ("in_progress", "decision", "blockers", "completed", "context"):
            for value in data[key]:
                Check.true(re.match(r"^\[((?:[SP]\d+)(?:[ ,]+[SP]\d+)*|all)\]\s+\S", value), "%s value lacks a tag: %r" % (key, value))
                Check.true(len(value) <= 500, "%s value too long" % key)
        Check.true(data["in_progress"], "no in_progress values")

    def digest_limits(self) -> None:
        # max_chars below 20,000 is refused; the focus copy is byte-identical and at most 20,000 bytes.
        out = self.base / "d5"
        out.mkdir()
        try:
            DigestWriter(out, self.base, 19999, FOCUS).write([], Pipeline.collect_result([]), {})
        except ValueError as error:
            Check.true("max-chars" in str(error), "message: %s" % error)
        else:
            raise AssertionError("max_chars 19999 was accepted")
        out, _s, _w, _i = self._write_digest("d5b", self._digest_sessions(self.base / "dhome", 2))
        copy = (out / "unfinished-work-checklist.md").read_bytes()
        Check.equal(copy, FOCUS.read_bytes(), "focus copy")
        Check.true(len(copy) <= 20000, "focus file over 20,000 bytes")

    def digest_short_lines_reserved(self) -> None:
        # Every open item gets at least a short line when they fit, and measuring them leaves the redaction counts as they were.
        out, summary, writer, items = self._write_digest("d6", self._digest_sessions(self.base / "dhome", 50))
        shown = summary["in_digest"]["full_blocks"] + summary["in_digest"]["short_lines"]
        Check.true(summary["unfinished"] >= 50, "sessions folded unexpectedly: %r" % summary["counts"])
        Check.equal(summary["in_digest"]["not_shown"], 0, "open items not shown")
        Check.equal(shown, summary["unfinished"], "shown items")
        before = dict(writer.send.counts)
        writer._short_lines_size([i for i in items if i.status in (STILL_OPEN, UNKNOWN)])
        Check.equal(dict(writer.send.counts), before, "redaction counts after a discarded rendering")

    def digest_pasted(self) -> None:
        # A pasted skill is left out of the digest's request lines, and the line says so.
        sessions = [SessionBuilder.make([PASTE + "\n" + r], sid="p%d" % i, cwd=str(self.work), start=i * 100, end="interrupted") for i, r in enumerate(["Explain PR 502 to me", "Investigate the flaky login test", "Draft the release notes"])]
        out, _s, _w, _i = self._write_digest("d7", sessions)
        text = (out / "digest.md").read_text(encoding="utf-8")
        Check.true("pasted instructions left out" in text, "no paste note")
        Check.true("Core principle" not in text, "pasted text reached the digest")

    # ---- report ---------------------------------------------------------------------------------

    def _report_folder(self, name: str, with_result: bool = True) -> Path:
        # A hand-made sessions.json (and result.json) covering every report case.
        out = self.base / name
        out.mkdir()

        def item(tag, status, project, last, title):
            # One sessions.json item with a resume entry.
            return {"tag": tag, "status": status, "reason": "reason for %s" % tag, "harness": "claude-code", "session_id": "id-%s" % tag, "cwd": "/code/%s" % project,
                    "project": project, "title": title, "last": last, "resume": {"cwd": "/code/%s" % project, "command": "claude --resume id-%s" % tag,
                    "bash": 'cd "/code/%s" && claude --resume id-%s' % (project, tag), "powershell": 'Set-Location "/code/%s"; claude --resume id-%s' % (project, tag), "env": {}, "note": ""}}
        index = {"schema": 1, "window": {"since": None}, "counts": {"sessions": 9}, "items": [
            item("S1", "still open", "billing", "2026-10-10T10:00:00+00:00", "Retry backoff"),
            item("S2", "still open", "billing", "2026-10-12T10:00:00+00:00", "Retry backoff, part 2"),
            item("S3", "probably done", "docs", "2026-10-09T10:00:00+00:00", "Docs page"),
            dict(item("S4", "still open", "web", "2026-10-11T10:00:00+00:00", "Navbar fix"), left={"todos": ["Fix the navbar overflow"], "remaining": ["Next steps: test it on mobile."], "question": ""}),
            item("S5", "unknown", "api", "2026-10-08T10:00:00+00:00", "Old migration"),
        ], "prompts": [{"tag": "P1", "session": "S2", "index": 0, "text": "x"}]}
        Writer.text(out / "sessions.json", json.dumps(index))
        if with_result:
            ideas = [
                {"id": "idea-001", "title": "Finish the retry backoff", "summary": "[S1 S2] billing, last touched Oct 12.", "first_action": "Resume [S2]", "completion_criteria": "Merged."},
                {"id": "idea-002", "title": "Answer the open question", "summary": "From [P1]."},
                {"id": "idea-003", "title": "Clean up everything", "summary": "No tags here."},
                {"id": "idea-004", "title": "Check the ghost task", "summary": "See [S99]."},
                {"id": "idea-005", "title": "Finish the docs page", "summary": "[S3] docs."},
            ]
            Writer.text(out / "result.json", json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": {"status": "complete", "stop_reason": "completed", "ideas": ideas}}))
        return out

    def report_grouping(self) -> None:
        # [S1 S2] groups under billing with both resume commands, newest first; [P1] resolves to S2.
        out = self._report_folder("r1")
        summary = ChecklistReporter(out, "bash").build()
        by_id = {e["idea_id"]: e for e in summary["items"]}
        Check.equal(by_id["idea-001"]["sessions"], ["S2", "S1"], "idea-001 sessions")
        Check.equal(by_id["idea-001"]["project"], "billing", "idea-001 project")
        Check.equal(by_id["idea-002"]["sessions"], ["S2"], "P1 resolution")
        text = (out / "checklist.md").read_text(encoding="utf-8")
        Check.true('cd "/code/billing" && claude --resume id-S1' in text and "claude --resume id-S2" in text, "resume commands missing")

    def report_untraced(self) -> None:
        # An idea with no tags goes under "Not traced to a session" and is flagged; [S99] is an unknown tag.
        out = self._report_folder("r2")
        summary = ChecklistReporter(out, "bash").build()
        by_id = {e["idea_id"]: e for e in summary["items"]}
        Check.true(not by_id["idea-003"]["grounded"] and by_id["idea-003"]["flags"], "idea-003 not flagged")
        Check.true("idea-003" in summary["ungrounded"], "ungrounded list")
        Check.equal(by_id["idea-004"]["unknown_tags"], ["S99"], "unknown tags")
        text = (out / "checklist.md").read_text(encoding="utf-8")
        Check.true(text.index("## Not traced to a session") < text.index("Clean up everything"), "untraced idea not under its group")
        Check.true(text.index("## Not traced to a session") > text.index("## billing"), "untraced group not last")

    def report_probably_done(self) -> None:
        # An idea citing only a probably-done session is flagged.
        summary = ChecklistReporter(self._report_folder("r3"), "bash").build()
        flags = {e["idea_id"]: e["flags"] for e in summary["items"]}["idea-005"]
        Check.true(any("looks finished" in f for f in flags), "flags: %r" % flags)

    def report_added_and_order(self) -> None:
        # An open session no idea cites is added under its project; projects are newest first; items start with "- [ ] ".
        out = self._report_folder("r4")
        summary = ChecklistReporter(out, "powershell").build()
        added = [e["tag"] for e in summary["added_from_local_check"]]
        Check.equal(added, ["S4", "S5"], "added items")
        text = (out / "checklist.md").read_text(encoding="utf-8")
        headings = [line[3:] for line in text.splitlines() if line.startswith("## ")]
        Check.equal(headings, ["billing", "web", "docs", "api", "Not traced to a session"], "project order")
        Check.true('Set-Location "/code/web"; claude --resume id-S4' in text, "PowerShell resume command missing")
        Check.true("Open todos: Fix the navbar overflow" in text and "Next steps: test it on mobile." in text, "what is left is missing from a local item")
        for line in text.splitlines():
            if line.startswith("- ["):
                Check.true(line.startswith("- [ ] "), "bad item line: %r" % line)

    def report_local_only(self) -> None:
        # --local-only builds the checklist from sessions.json alone, listing every open and unknown session.
        out = self._report_folder("r5", with_result=False)
        summary = ChecklistReporter(out, "bash", local_only=True).build()
        Check.equal([e["tag"] for e in summary["added_from_local_check"]], ["S2", "S4", "S1", "S5"], "local-only items")
        Check.equal(summary["counts"]["ideas"], 0, "ideas")
        Check.true("without a Suggestion Agent run" in (out / "checklist.md").read_text(encoding="utf-8"), "intro")


class Home:
    """A temporary home with Claude Code and Codex stores and real project folders."""

    def __init__(self, base: Path, git: Git) -> None:
        # Builds the folders, a git repository with a remote, and every fixture session.
        self.path = base / "home"
        self.repo = git.repo_with_remote(self.path / "code", "billing")
        Writer.text(self.repo / "retry.py", "# work in progress\n")
        self.notes = self.path / "code" / "notes"
        self.notes.mkdir(parents=True)
        self.plain = self.path / "code" / "plain"
        self.plain.mkdir(parents=True)
        self.forms = self.path / "code" / "forms"
        self.forms.mkdir(parents=True)
        self.pi_proj = self.path / "code" / "pi-proj"
        self.pi_proj.mkdir(parents=True)
        self.build()

    def build(self) -> None:
        # Writes the Claude Code and Codex fixtures.
        projects = self.path / ".claude" / "projects"
        Writer.jsonl(projects / "C--billing" / "claude-open.jsonl", self.claude_open(str(self.repo)), 30)
        Writer.jsonl(projects / "C--plain" / "claude-done.jsonl", self.claude_done(str(self.plain)), 40)
        Writer.jsonl(projects / "C--billing" / "claude-pr.jsonl", self.claude_pr(str(self.repo)), 50)
        codex = self.path / ".codex" / "sessions" / "2026" / "10" / "01"
        Writer.jsonl(codex / "rollout-int.jsonl", self.codex_interrupted(str(self.notes)), 205)
        Writer.jsonl(codex / "rollout-early.jsonl", self.codex_forms("codex-early", str(self.forms), 300, finished=False), 305)
        Writer.jsonl(codex / "rollout-late.jsonl", self.codex_forms("codex-late", str(self.forms), 400, finished=True), 405)
        Writer.jsonl(self.path / ".pi" / "agent" / "sessions" / "--pi-proj--" / "2026-10-01_pi-1.jsonl", self.pi_aborted(str(self.pi_proj)), 505)

    @staticmethod
    def user(text, minutes: float, cwd: str) -> dict:
        # A Claude Code user record.
        return {"type": "user", "timestamp": Stamp.iso(minutes), "cwd": cwd, "sessionId": "x", "message": {"role": "user", "content": text}}

    @staticmethod
    def assistant(minutes: float, cwd: str, text: str = "", tools: list = ()) -> dict:
        # A Claude Code assistant record with text and tool_use parts.
        parts = ([{"type": "text", "text": text}] if text else []) + list(tools)
        return {"type": "assistant", "timestamp": Stamp.iso(minutes), "cwd": cwd, "message": {"role": "assistant", "model": "claude-opus-5-5", "content": parts, "stop_reason": None if tools else "end_turn", "id": "m%s" % minutes}}

    def claude_open(self, cwd: str) -> list:
        # Still open: open todos and a "next steps" ending, in a repository with uncommitted changes.
        todos = [{"content": "Add jitter", "status": "completed"}, {"content": "Wire the retry into the worker", "status": "pending"}]
        return [
            self.user("Add exponential backoff to the webhook retry loop in the billing worker, key %s" % SECRET_KEY, 20, cwd),
            self.assistant(21, cwd, "", tools=[{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": cwd + "/retry.py"}}, {"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {"todos": todos}}]),
            self.user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], 22, cwd),
            self.assistant(23, cwd, "Backoff added. Next steps: wire the retry into the worker and run the tests."),
            {"type": "ai-title", "aiTitle": "Webhook retry backoff", "sessionId": "x"},
        ]

    def claude_done(self, cwd: str) -> list:
        # Finished: one question, one answer.
        return [self.user("Explain how the cache eviction works", 35, cwd), self.assistant(36, cwd, "The cache evicts the least recently used entry.")]

    def claude_pr(self, cwd: str) -> list:
        # Interrupted after opening a pull request that the fake gh reports as merged.
        return [
            self.user("Open a pull request for the invoice rounding fix", 45, cwd),
            self.assistant(46, cwd, "", tools=[{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "gh pr create --fill"}}]),
            self.user([{"type": "tool_result", "tool_use_id": "t9", "content": "https://github.com/acme/billing/pull/12"}], 47, cwd),
            self.assistant(48, cwd, "Opened https://github.com/acme/billing/pull/12 for review."),
            self.user("[Request interrupted by user]", 49, cwd),
        ]

    @staticmethod
    def rec(kind: str, payload: dict, minutes: float) -> dict:
        # One Codex rollout line.
        return OrderedDict([("timestamp", Stamp.iso(minutes)), ("type", kind), ("payload", payload)])

    def codex_interrupted(self, cwd: str) -> list:
        # Still open: a failing test run, then the user aborted the turn.
        r = self.rec
        return [
            r("session_meta", OrderedDict([("id", "codex-int-1"), ("cwd", cwd), ("originator", "codex_cli_rs"), ("source", "cli")]), 200),
            r("event_msg", OrderedDict([("type", "user_message"), ("message", "Refactor src/db.py so the integration tests pass, mail me at %s" % SECRET_EMAIL)]), 201),
            r("response_item", OrderedDict([("type", "function_call"), ("name", "shell_command"), ("arguments", json.dumps({"command": ["bash", "-lc", "pytest -q"]})), ("call_id", "c1")]), 202),
            r("response_item", OrderedDict([("type", "function_call_output"), ("call_id", "c1"), ("output", "Exit code: 1\nFAILED")]), 202.5),
            r("event_msg", OrderedDict([("type", "turn_aborted"), ("reason", "interrupted")]), 205),
        ]

    @staticmethod
    def pi_aborted(cwd: str) -> list:
        # Still open: a Pi session whose last reply was aborted after a failing tool call.
        def m(minutes, message):
            return {"type": "message", "id": "m%s" % minutes, "timestamp": Stamp.iso(minutes), "message": message}
        return [
            {"type": "session", "version": 3, "id": "pi-1", "timestamp": Stamp.iso(500), "cwd": cwd},
            m(500.1, {"role": "user", "content": [{"type": "text", "text": "Port the CSV importer to the streaming parser"}]}),
            m(500.2, {"role": "assistant", "model": "deepseek-v4-pro", "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "npm test"}}], "stopReason": "toolUse"}),
            m(500.3, {"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": True, "content": [{"type": "text", "text": "2 failing"}]}),
            m(500.4, {"role": "assistant", "model": "deepseek-v4-pro", "content": [], "stopReason": "aborted"}),
        ]

    def codex_forms(self, session_id: str, cwd: str, minutes: float, finished: bool) -> list:
        # The same request twice: interrupted first, finished in a later session.
        r = self.rec
        records = [
            r("session_meta", OrderedDict([("id", session_id), ("cwd", cwd), ("originator", "codex_cli_rs"), ("source", "cli")]), minutes),
            r("event_msg", OrderedDict([("type", "user_message"), ("message", "Migrate the settings page to the new form library")]), minutes + 1),
        ]
        if finished:
            records.append(r("event_msg", OrderedDict([("type", "agent_message"), ("message", "Migrated and verified."), ("phase", "final_answer")]), minutes + 4))
            records.append(r("event_msg", OrderedDict([("type", "task_complete"), ("last_agent_message", "Migrated and verified.")]), minutes + 5))
        else:
            records.append(r("event_msg", OrderedDict([("type", "turn_aborted"), ("reason", "interrupted")]), minutes + 3))
        return records


class Runner:
    """Runs unfinished_work.py in a subprocess confined to the fixture home."""

    def __init__(self, home: Home, base: Path, fake_cli: tuple, fake_gh: tuple) -> None:
        # Builds the confined environment once; the fake gh folder leads PATH.
        self.home = home
        self.cli, self.cli_module, _bin = fake_cli
        _gh, self.gh_module, self.gh_bin = fake_gh
        self.log = base / "fakecli-log.jsonl"
        self.gh_log = base / "fakegh-int-log.jsonl"
        self.base = base
        self.env = self._env(base)

    def run(self, *args: str, extra_env: dict = None, with_gh: bool = True):
        # Returns (exit code, parsed stdout JSON or None, stdout text, stderr text).
        env = dict(self.env)
        if not with_gh:
            env["PATH"] = self.clean_path()
        env.update(extra_env or {})
        process = subprocess.run([sys.executable, str(ENTRY)] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        out = process.stdout.decode("utf-8", "replace")
        try:
            document = json.loads(out) if out.strip() else None
        except ValueError:
            document = None
        return process.returncode, document, out, process.stderr.decode("utf-8", "replace")

    def calls(self) -> list:
        # Every recorded fake vidbyte-cli call.
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _env(self, base: Path) -> dict:
        # HOME and every harness folder in the fixture home; PATH = the fake gh, then PATH without vidbyte-cli or gh.
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        h = self.home.path
        env.update({
            "HOME": str(h), "USERPROFILE": str(h), "CLAUDE_CONFIG_DIR": str(h / ".claude"), "CODEX_HOME": str(h / ".codex"),
            "XDG_DATA_HOME": str(h / ".local" / "share"), "APPDATA": str(h / "AppData" / "Roaming"), "LOCALAPPDATA": str(h / "AppData" / "Local"),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(base / "vb-state"), "FAKE_CLI_LOG": str(self.log), "FAKE_GH_LOG": str(self.gh_log),
            "FAKE_GH_AUTH": "0", "FAKE_GH_STATES": json.dumps({"12": "MERGED"}), "PYTHONPATH": os.pathsep.join([str(self.cli_module), str(self.gh_module)]),
            "PYTHONIOENCODING": "utf-8", "PATH": os.pathsep.join([str(self.gh_bin), self.clean_path()]),
        })
        return env

    @staticmethod
    def clean_path() -> str:
        # The current PATH minus every folder that holds a vidbyte-cli or a gh.
        names = ("vidbyte-cli", "vidbyte-cli.exe", "vidbyte-cli.cmd", "vidbyte-cli.bat", "gh", "gh.exe")
        keep = [f for f in os.environ.get("PATH", "").split(os.pathsep) if f and not any(os.path.exists(os.path.join(f, n)) for n in names)]
        return os.pathsep.join(keep)


class IntegrationTests:
    """Section 10 integration tests (subprocesses against the fixture home)."""

    def __init__(self, base: Path, runner: Runner) -> None:
        # Keeps the runner; gather's main output folder is created by the first test.
        self.base = base
        self.runner = runner
        self.out = base / "int-out"

    def gather_main(self) -> None:
        # gather over Claude Code and Codex: exit 0, counts per status, the four files, git used, gh used.
        code, doc, out, err = self.runner.run("gather", "--out", str(self.out))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-500:])
        Check.equal(doc["sessions_read"], 7, "sessions read")
        Check.equal(doc["by_harness"], {"claude-code": 3, "codex": 3, "pi": 1}, "sessions by harness")
        Check.equal(doc["counts"], {"candidates": 5, "still_open": 3, "unknown": 0, "probably_done": 2, "folded": 0}, "counts")
        for name in ("digest.md", "sessions.json", "inputs.json", "unfinished-work-checklist.md"):
            Check.true((self.out / name).is_file(), name + " missing")
        Check.equal((doc["checks"]["git"], doc["checks"]["gh"]), ("used", "used"), "checks")
        index = json.loads((self.out / "sessions.json").read_text(encoding="utf-8"))
        by_title = {e["title"]: e for e in index["items"]}
        Check.true("merged" in by_title["Open a pull request for the invoice rounding fix"]["reason"], "PR reason: %r" % by_title)
        Check.true("uncommitted" in by_title["Webhook retry backoff"]["reason"], "repo facts missing: %r" % by_title["Webhook retry backoff"]["reason"])
        Check.equal(by_title["Webhook retry backoff"]["left"]["todos"], ["Wire the retry into the worker"], "left todos")
        Check.true("same first request" in by_title["Migrate the settings page to the new form library"]["reason"], "later-session reason")
        pi = by_title["Port the CSV importer to the streaming parser"]
        Check.equal((pi["status"], pi["resume"]["command"]), ("still open", "pi --session pi-1"), "Pi item")
        digest = (self.out / "digest.md").read_text(encoding="utf-8")
        for needle in (SECRET_KEY, SECRET_EMAIL, str(self.runner.home.path), "codex-int-1"):
            Check.true(needle not in digest, "digest leaks %r" % needle)

    def gather_no_gh(self) -> None:
        # Without gh on PATH the summary says "not installed", and the PR item is no longer judged merged.
        code, doc, _out, err = self.runner.run("gather", "--out", str(self.base / "int-nogh"), with_gh=False)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        Check.equal(doc["checks"]["gh"], "not installed", "gh status")
        Check.equal(doc["checks"]["git"], "used", "git must still be found once gh folders leave PATH")
        Check.equal(doc["counts"]["probably_done"], 1, "probably done without gh")

    def gather_bad_opencode(self) -> None:
        # A non-SQLite opencode.db is a read[] entry with status "error"; the other stores are still read.
        bad = self.base / "oc-bad"
        Writer.text(bad / "opencode.db", "this is not a database" * 50)
        code, doc, _out, err = self.runner.run("gather", "--out", str(self.base / "int-oc"), "--opencode-dir", str(bad))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        entry = next(e for e in doc["read"] if e["harness"] == "opencode")
        Check.equal(entry["status"], "error", "opencode read status")
        Check.true(entry.get("error"), "no error text: %r" % entry)
        Check.true(str(self.base) not in json.dumps(entry), "read[] leaks the home path: %r" % entry)
        Check.equal(doc["sessions_read"], 7, "other stores still read")

    def gather_nothing(self) -> None:
        # Every harness disabled exits 3; a folder whose only session finished gives next "nothing_to_send".
        code, doc, _o, _e = self.runner.run("gather", "--out", str(self.base / "int-none"), "--no-claude", "--no-codex", "--no-opencode", "--no-gemini", "--no-qwen", "--no-openclaude", "--no-factory", "--no-pi")
        Check.equal(code, 3, "exit with every harness off")
        code, doc, _o, err = self.runner.run("gather", "--out", str(self.base / "int-done"), "--project-dir", str(self.runner.home.plain))
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        Check.equal((doc["unfinished"], doc["next"]), (0, "nothing_to_send"), "nothing to send")

    def gather_limits(self) -> None:
        # --max-sessions 1 reads at most one session; --out on a folder that holds a digest is refused.
        code, doc, _o, _e = self.runner.run("gather", "--out", str(self.base / "int-one"), "--max-sessions", "1")
        Check.equal(code, 0, "exit")
        Check.true(doc["sessions_read"] <= 1, "sessions read: %r" % doc["sessions_read"])
        code, _d, _o, err = self.runner.run("gather", "--out", str(self.out))
        Check.equal(code, 2, "reused --out")

    def _copy(self, name: str) -> Path:
        # A copy of gather's output folder for one suggest test.
        target = self.base / name
        shutil.copytree(str(self.out), str(target))
        return target

    def suggest_dry_run(self) -> None:
        # The recorded argv: root options first, 4 categories, count 15, horizon any, files, instructions, token cap, dry run; cwd is the folder.
        out = self._copy("s-dry")
        before = len(self.runner.calls())
        code, doc, _o, err = self.runner.run("suggest", "--out", str(out), "--dry-run", "--cli", self.runner.cli)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        calls = self.runner.calls()[before:]
        Check.equal(len(calls), 1, "calls")
        argv = calls[0]["argv"]
        Check.equal(argv[:5], ["--json", "--no-input", "agents", "suggest", "run"], "root options first")
        Check.equal([argv[i + 1] for i, a in enumerate(argv) if a == "--category"], ["completion", "continuation", "verification", "stop_or_defer"], "categories")
        for flag, value in (("--count", "15"), ("--horizon", "any"), ("--files", "digest.md"), ("--instructions", "unfinished-work-checklist.md"), ("--max-total-tokens", "400000")):
            Check.equal(argv[argv.index(flag) + 1], value, flag)
        Check.true(argv[-1] == "--dry-run", "dry run flag")
        Check.equal(os.path.normcase(os.path.realpath(calls[0]["cwd"])), os.path.normcase(os.path.realpath(str(out))), "cwd")
        Check.true(not (out / "paid-run.txt").exists(), "a dry run wrote paid-run.txt")
        Check.equal(doc["stop_reason"], "dry_run", "stop reason")

    def suggest_paid_once(self) -> None:
        # --paid writes paid-run.txt then result.json with a cost estimate; a second --paid exits 2 with no second call.
        out = self._copy("s-paid")
        code, doc, _o, err = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.runner.cli)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        Check.true((out / "paid-run.txt").exists() and (out / "result.json").exists(), "paid files")
        Check.true(doc["estimated_cost_usd"] and doc["estimated_cost_usd"] > 0, "cost: %r" % doc.get("estimated_cost_usd"))
        Check.true("--dry-run" not in self.runner.calls()[-1]["argv"], "a paid run passed --dry-run")
        before = len(self.runner.calls())
        code, _d, _o, _e = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.runner.cli)
        Check.equal(code, 2, "second paid run")
        Check.equal(len(self.runner.calls()), before, "a second paid call reached the CLI")
        code, doc, _o, err = self.runner.run("report", "--out", str(out), "--shell", "bash")
        Check.equal(code, 0, "report exit (stderr: %s)" % err[-300:])
        text = (out / "checklist.md").read_text(encoding="utf-8")
        Check.true("claude --resume" in text or "codex resume" in text, "no resume commands in the checklist")
        Check.true(doc["ungrounded"] == ["idea-002"], "ungrounded: %r" % doc["ungrounded"])
        Check.true(doc["added_from_local_check"], "no locally added items")
        self.paid = out

    def suggest_out_of_credits(self) -> None:
        # A fake exit 5 with an error document on stderr gives exit 5 and error.code.
        out = self._copy("s-five")
        code, doc, _o, _e = self.runner.run("suggest", "--out", str(out), "--paid", "--cli", self.runner.cli, extra_env={"FAKE_CLI_EXIT": "5"})
        Check.equal(code, 5, "exit")
        Check.equal(doc["error"]["code"], "insufficient_credits", "error code")

    def suggest_no_cli_and_count(self) -> None:
        # No --cli and no vidbyte-cli on PATH exits 127; --count 31 exits 2 before any CLI call.
        out = self._copy("s-misc")
        code, _d, _o, _e = self.runner.run("suggest", "--out", str(out), "--dry-run")
        Check.equal(code, 127, "missing CLI")
        before = len(self.runner.calls())
        code, _d, _o, _e = self.runner.run("suggest", "--out", str(out), "--dry-run", "--count", "31", "--cli", self.runner.cli)
        Check.equal(code, 2, "count 31")
        Check.equal(len(self.runner.calls()), before, "a CLI call despite --count 31")

    def feedback_accept(self) -> None:
        # feedback --accept sends the exact title and the default project; the reason is redacted.
        out = getattr(self, "paid", None)
        Check.true(out is not None, "the paid test did not run")
        code, doc, _o, err = self.runner.run("feedback", "--out", str(out), "--idea", "idea-001", "--accept", "--reason", "good, key %s" % SECRET_KEY, "--cli", self.runner.cli)
        Check.equal(code, 0, "exit (stderr: %s)" % err[-300:])
        argv = self.runner.calls()[-1]["argv"]
        Check.equal(argv[:5], ["--json", "agents", "suggest", "feedback", "accept"], "command")
        Check.equal(doc["verb"], "accept", "reported verb")
        Check.equal(argv[argv.index("--project") + 1], "unfinished-agent-work", "project")
        Check.equal(argv[argv.index("--suggestion") + 1], "Finish the webhook retry backoff and push the branch", "title")
        Check.true(SECRET_KEY not in " ".join(argv), "reason not redacted")


def main() -> int:
    # Builds the temporary world, runs every test, prints the summary, and removes the world.
    base = Path(tempfile.mkdtemp(prefix="ufw-test-"))
    results = Results()
    try:
        unit = UnitTests(base / "unit")
        for name, category in [
            ("detector_mid_turn", "Silent Failure"), ("detector_no_turn_ends", "Hidden Assumption"), ("detector_endings", "Silent Failure"),
            ("detector_todos", "Edge Case"), ("detector_remaining", "Silent Failure"), ("detector_no_reply", "Edge Case"),
            ("detector_failed_weights", "Edge Case"), ("detector_trivial", "Edge Case"), ("detector_branches_and_prs", "Hidden Failure"),
            ("detector_keywords", "Silent Failure"), ("detector_numbers", "Silent Failure"), ("detector_pasted_text", "Hidden Failure"),
            ("matcher_same_task", "Silent Failure"), ("matcher_direction_and_project", "Hidden Assumption"), ("matcher_folders", "Silent Failure"),
            ("matcher_same_opening", "Edge Case"), ("matcher_prefers_finished", "Silent Failure"), ("matcher_number_conflict", "Silent Failure"),
            ("matcher_pasted_opening", "Silent Failure"),
            ("status_chain", "Hidden Failure"), ("status_pull_requests", "Silent Failure"), ("status_later_but_unpushed", "Hidden Failure"),
            ("status_folder_and_running", "Edge Case"), ("status_no_probe", "Hidden Assumption"),
            ("probe_folder_states", "Silent Failure"), ("probe_branches", "Silent Failure"), ("probe_invalid_branch", "Hidden Assumption"),
            ("probe_non_repo_and_cap", "Edge Case"), ("probe_read_only", "Hidden Failure"), ("probe_gh", "Hidden Assumption"),
            ("resume_commands", "Silent Failure"),
            ("digest_budget_and_order", "Silent Failure"), ("digest_redaction", "Silent Failure"), ("digest_tags", "Hidden Failure"),
            ("digest_inputs", "Hidden Assumption"), ("digest_limits", "Edge Case"), ("digest_short_lines_reserved", "Silent Failure"),
            ("digest_pasted", "Silent Failure"),
            ("report_grouping", "Silent Failure"), ("report_untraced", "Silent Failure"), ("report_probably_done", "Hidden Failure"),
            ("report_added_and_order", "Silent Failure"), ("report_local_only", "Edge Case"),
        ]:
            results.run(name, category, getattr(unit, name))
        world = base / "int"
        world.mkdir()
        home = Home(world, Git(world))
        fake_cli = Launcher.make(world, "fakecli", FAKE_CLI_SOURCE, "vidbyte-cli")
        fake_gh = Launcher.make(world, "fakegh", FAKE_GH_SOURCE, "gh")
        integration = IntegrationTests(world, Runner(home, world, fake_cli, fake_gh))
        for name, category in [
            ("gather_main", "Integration"), ("gather_no_gh", "Hidden Assumption"), ("gather_bad_opencode", "Hidden Failure"),
            ("gather_nothing", "Edge Case"), ("gather_limits", "Edge Case"), ("suggest_dry_run", "Integration"),
            ("suggest_paid_once", "Hidden Failure"), ("suggest_out_of_credits", "Silent Failure"), ("suggest_no_cli_and_count", "Hidden Assumption"),
            ("feedback_accept", "Silent Failure"),
        ]:
            results.run(name, category, getattr(integration, name))
    finally:
        shutil.rmtree(str(base), ignore_errors=True)
    return results.finish()


if __name__ == "__main__":
    sys.exit(main())
