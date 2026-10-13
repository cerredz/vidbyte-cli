"""Finds, for each unfinished session, a later session in the same project that covers the same task.

A later session that finished the task makes the earlier one "probably done"; a later session that is
itself unfinished absorbs the earlier one, so one task becomes one checklist item. Matching is strict on
purpose: a false match hides real unfinished work, while a missed match only leaves one extra item.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from .repo_state import GitLocator
from .signals import FolderNames

MIN_OPENING_WORDS = 3
MIN_SHARE = 0.6  # Shared words must carry 60% of the distinctive weight of BOTH sessions' task words.
MIN_SHARED_WORDS = 3
ANCHOR_SHARE = 0.35  # With a shared pull request number or branch, 35% of both sides is enough.
FILE_PREFIX = "file:"
FLOOR = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


class ProjectKeys:
    """Decides which sessions belong to the same project: one git repository (worktrees included), else one folder."""

    def __init__(self, use_git: bool = True) -> None:
        # use_git=False (--no-git) treats every folder as its own project tree.
        self.use_git = use_git
        self._cache: dict = {}

    def key(self, session) -> str:
        # "git:<common dir>" for a folder inside a repository, else "dir:<folder>", else "" without a real folder.
        cwd = session.cwd
        if not FolderNames.is_real(cwd):
            return ""
        norm = FolderNames.norm(cwd)
        if norm not in self._cache:
            common = GitLocator.common_dir(cwd) if self.use_git else ""
            self._cache[norm] = ("git:" + FolderNames.norm(common)) if common else ("dir:" + norm)
        return self._cache[norm]

    def same(self, a, b) -> bool:
        # True when two sessions are in the same repository, or one folder contains the other.
        ka, kb = self.key(a), self.key(b)
        if not ka or not kb:
            return False
        if ka == kb:
            return True
        if ka.startswith("dir:") or kb.startswith("dir:"):
            return FolderNames.inside(a.cwd, b.cwd) or FolderNames.inside(b.cwd, a.cwd)
        return False


class TaskWeights:
    """Weighs task words by how rare they are across every session read, so generic words count for little."""

    def __init__(self, findings: list) -> None:
        # Counts, per word and touched file, how many sessions use it.
        self.total = max(len(findings), 1)
        self.counts: dict = {}
        for finding in findings:
            for token in self.tokens(finding):
                self.counts[token] = self.counts.get(token, 0) + 1

    @staticmethod
    def tokens(finding) -> set:
        # The finding's task words plus its touched files, prefixed so a file never equals a word.
        return set(finding.keywords) | {FILE_PREFIX + name for name in finding.files}

    def weight(self, token: str) -> float:
        # log(total / sessions using the token): near 0 for words every session uses, high for rare ones.
        return math.log(self.total / max(self.counts.get(token, 1), 1)) + 0.01

    def share(self, mine: set, theirs: set, shared: set) -> float:
        # The weight of the shared tokens as a fraction of the smaller-scoring side's total.
        shared_weight = sum(self.weight(t) for t in shared)
        sides = [sum(self.weight(t) for t in side) for side in (mine, theirs)]
        return min(shared_weight / side if side else 0.0 for side in sides)


class LaterMatch:
    """The later session judged to cover the same task as an earlier unfinished one."""

    __slots__ = ("finding", "kind", "shared_keywords", "shared_files", "finished")

    def __init__(self, finding, kind: str, shared_keywords: list, shared_files: list, finished: bool) -> None:
        # Holds the later finding, how it matched, what they share, and whether the later one finished.
        self.finding = finding
        self.kind = kind
        self.shared_keywords = shared_keywords
        self.shared_files = shared_files
        self.finished = finished

    def evidence(self) -> str:
        # A short phrase saying why the two sessions are the same task.
        if self.kind == "same_opening":
            return "same first request"
        parts = []
        if self.shared_files:
            parts.append("files " + ", ".join(self.shared_files[:3]))
        if self.shared_keywords:
            parts.append("words " + ", ".join(self.shared_keywords[:5]))
        return "shared " + "; ".join(parts) if parts else "same task"


class LaterWorkMatcher:
    """Matches each candidate finding to the best later finding of the same task in the same project."""

    def __init__(self, project_keys: ProjectKeys) -> None:
        # project_keys decides which sessions share a project.
        self.keys = project_keys
        self.weights = TaskWeights([])

    def match(self, findings: list) -> dict:
        # Returns {id(candidate finding): LaterMatch} for every candidate with a later match.
        self.weights = TaskWeights(findings)
        matches = {}
        for members in self._groups(findings).values():
            for finding in members:
                if not finding.candidate:
                    continue
                best = self._best_later(finding, members)
                if best is not None:
                    matches[id(finding)] = best
        return matches

    def _groups(self, findings: list) -> dict:
        # Groups findings by project key, dropping sessions without a real folder.
        groups = defaultdict(list)
        for finding in findings:
            key = self.keys.key(finding.session)
            if key:
                groups[key].append(finding)
        return self._merge_nested_folders(groups)

    def _merge_nested_folders(self, groups: dict) -> dict:
        # Merges folder groups (no git) where one folder is inside another, so a subfolder session joins its project.
        folder_keys = sorted((k for k in groups if k.startswith("dir:")), key=len)
        for i, outer in enumerate(folder_keys):
            if outer not in groups:
                continue
            for inner in folder_keys[i + 1:]:
                if inner in groups and FolderNames.inside(inner[4:], outer[4:]):
                    groups[outer].extend(groups.pop(inner))
        return groups

    def _best_later(self, finding, members: list):
        # The most recent finished match if any, else the most recent unfinished match, else None.
        session = finding.session
        finished, unfinished = None, None
        for other in members:
            if other is finding or not self._is_later(other.session, session):
                continue
            match = self._compare(finding, other)
            if match is None:
                continue
            if match.finished:
                if finished is None or self._last(other.session) > self._last(finished.finding.session):
                    finished = match
            elif unfinished is None or self._last(other.session) > self._last(unfinished.finding.session):
                unfinished = match
        return finished or unfinished

    def _compare(self, finding, other):
        # Returns a LaterMatch when other covers the same task as finding, else None.
        finished = not other.candidate
        if finding.opening and len(finding.opening.split()) >= MIN_OPENING_WORDS and finding.opening == other.opening:
            return LaterMatch(other, "same_opening", [], [], finished)
        if finding.numbers and other.numbers and not (finding.numbers & other.numbers):
            return None  # Both name pull requests or issues, and none in common: different tasks.
        mine, theirs = TaskWeights.tokens(finding), TaskWeights.tokens(other)
        shared = mine & theirs
        share = self.weights.share(mine, theirs, shared)
        anchored = bool(finding.numbers & other.numbers) or bool(set(finding.branches) & set(other.branches))
        if (share >= MIN_SHARE and len(shared) >= MIN_SHARED_WORDS) or (anchored and share >= ANCHOR_SHARE):
            ranked = sorted(shared, key=self.weights.weight, reverse=True)
            words = [t for t in ranked if not t.startswith(FILE_PREFIX)]
            files = [t[len(FILE_PREFIX):] for t in ranked if t.startswith(FILE_PREFIX)]
            return LaterMatch(other, "same_task", words, files, finished)
        return None

    @staticmethod
    def _is_later(later, earlier) -> bool:
        # True when later started after earlier started and was active after earlier's last activity.
        if later.first_ts is None or earlier.first_ts is None or later.last_ts is None or earlier.last_ts is None:
            return False
        return later.first_ts > earlier.first_ts and later.last_ts > earlier.last_ts

    @staticmethod
    def _last(session):
        # The session's last activity, or the oldest possible time.
        return session.last_ts or FLOOR
