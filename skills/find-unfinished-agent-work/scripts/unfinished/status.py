"""Turns each candidate's signals, its later sessions, and the repository as it is now into one status.

Statuses: "still open", "probably done", "unknown", or "folded" (the same task continued in a later
unfinished session, which carries it). Every status comes with a one-line reason naming its evidence.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sessionkit.model import Clock

from .repo_state import GitLocator
from .signals import FolderNames

STILL_OPEN = "still open"
PROBABLY_DONE = "probably done"
UNKNOWN = "unknown"
FOLDED = "folded"
RECENT_MINUTES = 15
MAX_REASON_CHARS = 320
FLOOR = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


class Item:
    """One candidate session with its status, reason, repository facts, and any folded earlier sessions."""

    def __init__(self, finding) -> None:
        # Starts undecided; StatusJudge fills every field.
        self.finding = finding
        self.status = STILL_OPEN
        self.reason = ""
        self.facts: list = []
        self.folded: list = []
        self.head = None
        self.later = None
        self.folder = None
        self.branches: list = []
        self.pulls: list = []
        self.folder_gone = False
        self.may_be_running = False
        self.project = ""
        self.worktree = ""
        self.tag = None

    @property
    def session(self):
        # The sessionkit Session behind this item.
        return self.finding.session

    @property
    def last(self):
        # Last activity of the session, or the oldest possible time.
        return self.session.last_ts or FLOOR

    def all_branches(self) -> list:
        # This session's branch states plus those of every folded session.
        found = list(self.branches)
        for item in self.folded:
            found.extend(item.branches)
        return found

    def all_pulls(self) -> list:
        # This session's pull request states plus those of every folded session, without duplicates.
        seen, found = set(), []
        for pull in list(self.pulls) + [p for item in self.folded for p in item.pulls]:
            if pull.url not in seen:
                seen.add(pull.url)
                found.append(pull)
        return found


class StatusJudge:
    """Applies the status rules, in order, to every candidate finding."""

    def __init__(self, probe=None, now=None) -> None:
        # probe is a RepoStateProbe or None (--no-git); now is aware UTC, None for the real clock.
        self.probe = probe
        self.now = now or Clock.now()

    def judge(self, findings: list, matches: dict) -> list:
        # Returns every candidate's Item, newest first; folded items are reachable through their head.
        items = {id(f): Item(f) for f in findings if f.candidate}
        ordered = sorted(items.values(), key=lambda i: i.last, reverse=True)
        for item in ordered:
            self._probe(item)
            item.later = matches.get(id(item.finding))
        newest_in_folder = self._newest_per_folder(ordered)
        self._fold_chains(ordered, items)
        for item in ordered:
            if item.status != FOLDED:
                self._decide(item, newest_in_folder)
        for item in ordered:
            if item.status == FOLDED:
                item.reason = "the same task continued in a later unfinished session"
        return ordered

    def _probe(self, item: Item) -> None:
        # Fills folder, branch, and pull request facts plus the project label for one item.
        session = item.session
        cwd = session.cwd
        real = FolderNames.is_real(cwd)
        item.folder_gone = real and not Path(cwd).is_dir()
        if real and self.probe is not None and not item.folder_gone:
            item.folder = self.probe.folder(cwd)
            item.branches = [self.probe.branch(item.folder, name) for name in item.finding.branches] if item.folder else []
        if self.probe is not None:
            item.pulls = [p for p in (self.probe.pull(url, cwd) for url in item.finding.pr_urls) if p is not None]
        item.may_be_running = session.end_state == "working" and session.last_ts is not None and self.now - session.last_ts < dt.timedelta(minutes=RECENT_MINUTES)
        item.project, item.worktree = self._project_label(cwd, item.folder)

    @staticmethod
    def _project_label(cwd: str, folder) -> tuple:
        # (project, worktree): the repository's folder name, and the worktree folder name when it differs.
        if not cwd:
            return "(no folder)", ""
        if not FolderNames.is_real(cwd):
            return cwd, ""
        common = folder.common_dir if folder is not None and folder.common_dir else GitLocator.common_dir(cwd)
        here = FolderNames.base(folder.toplevel if folder is not None and folder.toplevel else cwd)
        if common:
            path = Path(common)
            project = path.parent.name if path.name == ".git" else path.name
            return project or here, (here if here and here != project else "")
        return here or cwd, ""

    @staticmethod
    def _newest_per_folder(ordered: list) -> dict:
        # Maps each normalized folder to the newest candidate in it, which owns the folder's current state.
        newest = {}
        for item in ordered:
            key = FolderNames.norm(item.session.cwd)
            if key and key not in newest:
                newest[key] = item
        return newest

    @staticmethod
    def _fold_chains(ordered: list, items: dict) -> None:
        # Follows "continued in a later unfinished session" links to their newest item and folds the rest into it.
        for item in ordered:
            head, seen = item, {id(item)}
            while head.later is not None and not head.later.finished and id(head.later.finding) in items:
                following = items[id(head.later.finding)]
                if id(following) in seen:
                    break
                seen.add(id(following))
                head = following
            if head is not item:
                item.status = FOLDED
                item.head = head
                head.folded.append(item)

    def _decide(self, item: Item, newest_in_folder: dict) -> None:
        # Applies the status rules in order and writes the reason.
        branches, pulls = item.all_branches(), item.all_pulls()
        unpushed = [b for b in branches if b.exists and b.unpushed]
        gone = [b for b in branches if b.exists is False]
        merged = [p for p in pulls if p.state == "MERGED"]
        open_prs = [p for p in pulls if p.state == "OPEN"]
        closed = [p for p in pulls if p.state == "CLOSED"]
        later_done = item.later is not None and item.later.finished
        item.facts = self._facts(item, newest_in_folder, unpushed, gone)
        if item.may_be_running:
            minutes = max(int((self.now - item.session.last_ts).total_seconds() // 60), 0)
            self._set(item, UNKNOWN, "it was active %d minutes ago and may still be running in another terminal" % minutes)
        elif merged:
            self._set(item, PROBABLY_DONE, "its pull request %s is merged" % self._pr_name(merged[-1]))
        elif open_prs:
            draft = " (draft)" if open_prs[-1].is_draft else ""
            self._set(item, STILL_OPEN, "its pull request %s is still open%s; %s" % (self._pr_name(open_prs[-1]), draft, item.finding.why()))
        elif closed:
            self._set(item, UNKNOWN, "its pull request %s was closed without merging" % self._pr_name(closed[-1]))
        elif later_done and unpushed:
            self._set(item, UNKNOWN, "a later session finished the same task (%s), but branch %s still has %d commit(s) on no remote" % (item.later.evidence(), unpushed[0].name, unpushed[0].unpushed))
        elif later_done:
            self._set(item, PROBABLY_DONE, "a later session in the same project finished the same task (%s)" % item.later.evidence())
        elif item.folder_gone:
            self._set(item, UNKNOWN, "the folder no longer exists; a removed worktree usually means the work was merged or dropped")
        elif gone and not unpushed and item.folder is not None and item.folder.dirty == 0 and not item.finding.strong:
            self._set(item, UNKNOWN, "branch %s no longer exists and the folder is clean" % gone[0].name)
        else:
            self._set(item, STILL_OPEN, item.finding.why())

    def _facts(self, item: Item, newest_in_folder: dict, unpushed: list, gone: list) -> list:
        # Plain-English facts about the repository now, for the reason and the digest.
        facts = []
        folder = item.folder
        if self.probe is None or self.probe.git is None:
            facts.append("git not checked")
        elif folder is not None and folder.is_repo:
            owner = newest_in_folder.get(FolderNames.norm(item.session.cwd)) is item
            if folder.dirty:
                facts.append("the folder has %d uncommitted change%s now%s" % (folder.dirty, "" if folder.dirty == 1 else "s", "" if owner else " (may belong to a later session)"))
            if owner and folder.branch != "HEAD" and folder.has_upstream is False and folder.has_remote:
                facts.append("its folder is on branch %s, which has no upstream" % folder.branch)
            if owner and folder.ahead:
                facts.append("branch %s is %d commit%s ahead of its upstream" % (folder.branch, folder.ahead, "" if folder.ahead == 1 else "s"))
        elif folder is not None and folder.exists and not folder.is_repo:
            facts.append("the folder is not a git repository")
        for branch in unpushed:
            facts.append("branch %s has %d commit%s on no remote" % (branch.name, branch.unpushed, "" if branch.unpushed == 1 else "s"))
        for branch in gone:
            facts.append("branch %s no longer exists" % branch.name)
        return facts

    @staticmethod
    def _pr_name(pull) -> str:
        # "#12" when gh reported a number, else the URL.
        return "#%s" % pull.number if pull.number else pull.url

    @staticmethod
    def _set(item: Item, status: str, primary: str) -> None:
        # Sets the status and a reason made of the deciding evidence followed by the repository facts.
        parts = [primary] + [f for f in item.facts if f not in primary]
        reason = "; ".join(p for p in parts if p)
        item.status = status
        item.reason = reason if len(reason) <= MAX_REASON_CHARS else reason[: MAX_REASON_CHARS - 1].rstrip() + "…"
