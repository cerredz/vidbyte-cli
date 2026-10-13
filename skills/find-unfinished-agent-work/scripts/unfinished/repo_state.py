"""Read-only checks of a session's folder, branches, and pull requests, as they are now.

Git runs with GIT_OPTIONAL_LOCKS=0 (status does not refresh the index), GIT_TERMINAL_PROMPT=0, and
`-c core.fsmonitor=false` (a repository's fsmonitor hook cannot run), without a shell and with stdin
closed. gh runs only `auth status` and `pr view`. Any failure leaves the fact unknown; nothing here
raises to the caller.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT = 10
GH_TIMEOUT = 15
MAX_DIRTY = 9999
MAX_PULL_LOOKUPS = 40
BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
PR_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")


class FolderState:
    """The state of one working folder now: does it exist, is it a repository, what is uncommitted."""

    def __init__(self, path: str) -> None:
        # Starts with every fact unknown.
        self.path = path
        self.exists = False
        self.is_repo = False
        self.toplevel = ""
        self.common_dir = ""
        self.branch = ""
        self.dirty = None
        self.ahead = None
        self.behind = None
        self.has_upstream = None
        self.has_remote = None
        self.error = ""

    def to_json(self) -> dict:
        # The facts as a JSON-ready dict (local index only; paths are not redacted here).
        return {
            "exists": self.exists, "is_repo": self.is_repo, "toplevel": self.toplevel, "branch": self.branch,
            "dirty": self.dirty, "ahead": self.ahead, "behind": self.behind, "has_upstream": self.has_upstream,
            "has_remote": self.has_remote, "error": self.error,
        }


class BranchState:
    """Whether a branch a session created still exists, and how many of its commits are on no remote."""

    def __init__(self, name: str) -> None:
        # Starts with every fact unknown.
        self.name = name
        self.exists = None
        self.unpushed = None
        self.error = ""

    def to_json(self) -> dict:
        # The facts as a JSON-ready dict.
        return {"name": self.name, "exists": self.exists, "unpushed": self.unpushed, "error": self.error}


class PullState:
    """The state of one pull request a session opened, as gh reports it."""

    def __init__(self, url: str) -> None:
        # Starts with an unknown state.
        self.url = url
        self.number = None
        self.state = None
        self.is_draft = None
        self.error = ""

    def to_json(self) -> dict:
        # The facts as a JSON-ready dict.
        return {"url": self.url, "number": self.number, "state": self.state, "is_draft": self.is_draft, "error": self.error}


class GitLocator:
    """Finds a folder's shared git directory by reading .git files, without running git."""

    MAX_LEVELS = 40

    @staticmethod
    def common_dir(path: str) -> str:
        # The repository's common .git folder (the same for every worktree of one repository), or "".
        try:
            current = Path(path)
            if not current.is_dir():
                return ""
            for _level in range(GitLocator.MAX_LEVELS):
                marker = current / ".git"
                if marker.is_dir():
                    return str(marker.resolve())
                if marker.is_file():
                    return GitLocator._from_gitfile(marker)
                if current.parent == current:
                    return ""
                current = current.parent
        except OSError:
            return ""
        return ""

    @staticmethod
    def _from_gitfile(marker: Path) -> str:
        # Reads "gitdir: <path>" from a worktree's .git file and returns the common folder above worktrees/.
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not text.startswith("gitdir:"):
            return ""
        gitdir = Path(text[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = (marker.parent / gitdir)
        commondir_file = gitdir / "commondir"
        try:
            if commondir_file.is_file():
                common = Path(commondir_file.read_text(encoding="utf-8", errors="replace").strip())
                return str((common if common.is_absolute() else gitdir / common).resolve())
        except OSError:
            pass
        if gitdir.parent.name == "worktrees":
            return str(gitdir.parent.parent.resolve())
        return str(gitdir.resolve())


class CommandRunner:
    """Runs one external command without a shell, with a timeout, and never raises."""

    def __init__(self, env_extra: dict) -> None:
        # env_extra is merged over the current environment for every command.
        self.env = dict(os.environ)
        self.env.update(env_extra)

    def run(self, argv: list, cwd: str = None, timeout: int = GIT_TIMEOUT):
        # Returns (exit code or None, stdout text, stderr text).
        try:
            done = subprocess.run(
                argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return None, "", type(exc).__name__
        return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


class RepoStateProbe:
    """Answers "is this work still open?" from the repository as it is now, read-only and cached."""

    GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}
    GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GH_PAGER": "cat"}

    def __init__(self, enabled_git: bool = True, enabled_gh: bool = True, max_folders: int = 80, git_exe: str = None, gh_exe: str = None) -> None:
        # Resolves git and gh from PATH unless paths are given; a disabled or missing tool is reported, not raised.
        self.git = (git_exe or shutil.which("git")) if enabled_git else None
        self.gh_path = (gh_exe or shutil.which("gh")) if enabled_gh else None
        self.git_status = "used" if self.git else ("disabled" if not enabled_git else "not installed")
        self.gh_status = "unchecked" if self.gh_path else ("disabled" if not enabled_gh else "not installed")
        self.max_folders = max_folders
        self.runner = CommandRunner(dict(self.GIT_ENV, **self.GH_ENV))
        self._folders: dict = {}
        self._remotes: dict = {}
        self._branches: dict = {}
        self._pulls: dict = {}
        self._gh_ok = None

    def folder(self, cwd: str):
        # The FolderState of cwd, or None when git is off or the folder cap is reached.
        if not self.git or not cwd:
            return None
        key = self._key(cwd)
        if key in self._folders:
            return self._folders[key]
        if len(self._folders) >= self.max_folders:
            return None
        state = FolderState(cwd)
        self._folders[key] = state
        self._inspect_folder(state)
        return state

    def branch(self, folder, name: str) -> BranchState:
        # Whether branch name exists in folder's repository and how many of its commits are on no remote.
        state = BranchState(name)
        if folder is None or not folder.is_repo:
            state.error = "no repository"
            return state
        if not self.valid_branch(name):
            state.error = "invalid branch name"
            return state
        key = (folder.common_dir or folder.toplevel, name)
        if key in self._branches:
            return self._branches[key]
        self._branches[key] = state
        code, _out, _err = self._git(folder.path, "rev-parse", "--verify", "--quiet", "refs/heads/" + name)
        if code is None:
            state.error = "git failed"
            return state
        state.exists = code == 0
        if state.exists:
            if folder.has_remote:
                count = self._git_int(folder.path, "rev-list", "--count", "refs/heads/" + name, "--not", "--remotes")
            else:
                count = self._git_int(folder.path, "rev-list", "--count", "refs/heads/" + name, "--not", "--exclude=refs/heads/" + name, "--branches")
            state.unpushed = count
        return state

    def pull(self, url: str, cwd: str = None):
        # The PullState of a pull request URL, or None when gh is off, missing, logged out, or over the lookup cap.
        if not self.gh_ready() or not PR_URL_RE.match(url or ""):
            return None
        if url in self._pulls:
            return self._pulls[url]
        if len(self._pulls) >= MAX_PULL_LOOKUPS:
            return None
        state = PullState(url)
        self._pulls[url] = state
        run_dir = cwd if cwd and Path(cwd).is_dir() else None
        code, out, err = self.runner.run([self.gh_path, "pr", "view", url, "--json", "number,state,isDraft,mergedAt"], cwd=run_dir, timeout=GH_TIMEOUT)
        if code != 0:
            state.error = (err.strip().splitlines() or ["gh failed"])[0][:160]
            return state
        try:
            data = json.loads(out)
        except ValueError:
            state.error = "gh printed no JSON"
            return state
        state.number = data.get("number")
        state.state = str(data.get("state") or "").upper() or None
        state.is_draft = data.get("isDraft")
        return state

    def gh_ready(self) -> bool:
        # True once `gh auth status` exited 0; checked at most once per run.
        if self.gh_path is None:
            return False
        if self._gh_ok is None:
            code, _out, _err = self.runner.run([self.gh_path, "auth", "status"], timeout=GH_TIMEOUT)
            self._gh_ok = code == 0
            self.gh_status = "used" if self._gh_ok else "not logged in"
        return self._gh_ok

    def summary(self) -> dict:
        # What was checked, for the gather summary and the digest header.
        return {
            "git": self.git_status,
            "gh": self.gh_status,
            "folders_checked": len(self._folders),
            "folder_cap": self.max_folders,
            "pull_requests_checked": len(self._pulls),
        }

    @staticmethod
    def valid_branch(name: str) -> bool:
        # A safe branch name: no leading dash, no "..", only common characters.
        return bool(name) and not name.startswith("-") and ".." not in name and bool(BRANCH_NAME_RE.match(name))

    def _inspect_folder(self, state: FolderState) -> None:
        # Fills existence, repository identity, branch, uncommitted entries, and upstream distance.
        if not Path(state.path).is_dir():
            return
        state.exists = True
        code, out, err = self._git(state.path, "rev-parse", "--show-toplevel", "--git-common-dir")
        if code != 0:
            state.error = self._first_line(err) or "not a git repository"
            return
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) < 2:
            state.error = "unexpected rev-parse output"
            return
        state.is_repo = True
        state.toplevel = lines[0]
        common = Path(lines[1])
        state.common_dir = str(common if common.is_absolute() else (Path(state.path) / common).resolve())
        code, out, _err = self._git(state.path, "symbolic-ref", "--short", "-q", "HEAD")
        state.branch = out.strip() if code == 0 and out.strip() else "HEAD"
        code, out, _err = self._git(state.path, "status", "--porcelain")
        if code == 0:
            state.dirty = min(len([line for line in out.splitlines() if line.strip()]), MAX_DIRTY)
        state.has_remote = self._has_remote(state)
        if state.branch != "HEAD":
            code, out, _err = self._git(state.path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
            parts = out.split()
            if code == 0 and len(parts) == 2 and all(p.isdigit() for p in parts):
                state.has_upstream, state.behind, state.ahead = True, int(parts[0]), int(parts[1])
            elif code is not None:
                state.has_upstream = False

    def _has_remote(self, state: FolderState):
        # True when the repository has at least one remote, cached per repository.
        key = state.common_dir or state.toplevel
        if key not in self._remotes:
            code, out, _err = self._git(state.path, "remote")
            self._remotes[key] = (bool(out.strip()) if code == 0 else None)
        return self._remotes[key]

    def _git(self, cwd: str, *args: str):
        # Runs one read-only git command in cwd.
        return self.runner.run([self.git, "-c", "core.fsmonitor=false", "-C", cwd] + list(args), timeout=GIT_TIMEOUT)

    def _git_int(self, cwd: str, *args: str):
        # Runs a git command that prints one integer; returns it, or None.
        code, out, _err = self._git(cwd, *args)
        text = out.strip()
        return int(text) if code == 0 and text.isdigit() else None

    @staticmethod
    def _first_line(text: str) -> str:
        # The first non-empty line of text, cut to 160 characters.
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()[:160]
        return ""

    @staticmethod
    def _key(path: str) -> str:
        # A folder path normalized for caching.
        value = str(path).replace("\\", "/").rstrip("/")
        return value.lower() if os.name == "nt" else value
