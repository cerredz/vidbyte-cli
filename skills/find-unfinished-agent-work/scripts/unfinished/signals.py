"""Detects sessions whose work was not finished, from what the session itself recorded.

Every check is local and free. A session becomes a candidate when the weights of its signals add up
to CANDIDATE_SCORE or more; the later-session matcher and the repository probe then decide whether
the work is still open.
"""

from __future__ import annotations

import os
import re

from sessionkit.model import TextTools

CANDIDATE_SCORE = 2
STRONG_SCORE = 4
MAX_TODOS_SHOWN = 8
MAX_REMAINING_LINES = 3
REMAINING_LINE_CHARS = 240
DONE_TODO_STATUSES = {"completed", "complete", "done", "cancelled", "canceled", "skipped"}
DEFAULT_BRANCH_NAMES = {"main", "master", "develop", "dev", "trunk", "head", "origin"}

REMAINING_RE = re.compile(
    r"\b(next steps?|remaining|still (?:need|needs|needed|to do|todo|have to|has to|pending|failing|broken|missing)"
    r"|left to do|not (?:yet|done|finished|complete|completed|implemented|tested|verified|pushed|merged)"
    r"|i (?:couldn't|could not|wasn't able|was not able|was unable|didn't get to|did not get to|haven't|have not)"
    r"|unable to|follow[- ]?ups?|blocked|blockers?|outstanding|didn't finish|did not finish|ran out of)\b",
    re.I,
)
TODO_WORD_RE = re.compile(r"\bTODO\b")
NEGATED_RE = re.compile(
    r"\b(?:no|nothing|none|zero|all)\s+(?:\w+\s+){0,2}?(?:remaining|left|pending|outstanding|blockers?|follow[- ]?ups?|todos?|next steps?)\b"
    r"|\bnothing (?:else )?(?:left|remaining|to do|outstanding)\b|\b(?:not|no longer|un)blocked\b|\bno (?:further )?follow[- ]?up (?:is )?needed\b",
    re.I,
)
OFFER_RE = re.compile(
    r"\b(?:also|as well|additionally|anything else|any other|further|too)\b|\blet me know if\b|\bhappy to\b|\bif you(?:'d| would) like\b|\bi can also\b",
    re.I,
)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b|create_pull_request", re.I)
TASK_NUMBER_RE = re.compile(
    r"(?<![\w&#])#(\d{1,6})(?![\w-])|\b(?:PRs?|pull requests?|issues?)\s*#?(\d{1,6})\b|/(?:pull|issues)/(\d{1,6})\b", re.I
)
PASTE_MIN_SESSIONS = 3  # A line found in the prompts of 3+ sessions is pasted text (a skill, a template), not a request.
PASTE_MIN_LINES = 4  # A prompt counts as pasted only when 4+ of its lines are such lines, so a retried request stays.
PASTE_LINE_CHARS = 12
PASTE_SCAN_CHARS = 20_000
SHELL_ACTION_RE = re.compile(r"^[\w.\-]+:\s+(.*)$")
BRANCH_RES = (
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?checkout\s+-[bB]\s+([^\s;&|'\"]+)"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?switch\s+(?:-c|-C|--create)\s+([^\s;&|'\"]+)"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?worktree\s+add\b[^;&|]*?\s-[bB]\s+([^\s;&|'\"]+)"),
    re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\s+(?:-u|--set-upstream)\s+[^\s;&|-][^\s;&|]*\s+([^\s;&|:'\"]+)"),
    re.compile(r"\bgh\s+pr\s+create\b[^;&|]*?--head\s+([^\s;&|'\"]+)"),
)
WORD_RE = re.compile(r"#\d+|[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_]+)*")
STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are aren't as at be because been before being below
    between both but by can can't cannot could couldn't did didn't do does doesn't doing don't done down during each
    few for from further had hadn't has hasn't have haven't having he her here hers him his how i i'm i've if in into
    is isn't it it's its itself just let let's lets me more most my myself no nor not now of off on once only or other
    our ours out over own same she should shouldn't so some such than that that's the their theirs them then there
    these they this those through to too under until up us very was wasn't we were weren't what when where which
    while who whom why will with won't would wouldn't you your yours yourself yes yeah ok okay thanks thank please hey
    hello hi sure great good nice cool right well got get gets getting go goes going gone make makes making made
    need needs want wants like use used using uses try trying tried look looking see seems seem think know way thing
    things stuff lot new one two three first last next another every everything something anything nothing
    file files code run runs running fix fixes fixed add adds added update updates updated change changes changed
    check checks checked help work works working worked implement implemented create created write wrote written
    read build built test tests testing tested error errors issue issues bug bugs feature features task tasks
    repo repository project branch commit commits push pushed pull merge merged main master step steps plan
    claude codex agent agents session sessions skill skills model prompt prompts tool tools command commands
    should've would've could've also again still actually really maybe probably basically just already
    can't don't won't isn't aren't didn't doesn't wasn't weren't i'll you'll we'll it'll that'll there's here's
    """.split()
)
EDIT_PREFIX = "Edit "


class Signal:
    """One reason a session looks unfinished, with its weight and a short piece of evidence."""

    __slots__ = ("id", "weight", "label", "detail")

    def __init__(self, signal_id: str, weight: int, label: str, detail: str = "") -> None:
        # Holds the signal id, its weight, a plain-English label, and raw (unredacted) evidence text.
        self.id = signal_id
        self.weight = weight
        self.label = label
        self.detail = detail


class Finding:
    """What the detector learned about one session: signals, score, and the facts later checks use."""

    def __init__(self, session) -> None:
        # Starts an empty finding for one sessionkit Session.
        self.session = session
        self.signals: list = []
        self.keywords: set = set()
        self.files: set = set()
        self.branches: list = []
        self.pr_urls: list = []
        self.mentioned_pr_urls: list = []
        self.first_request = ""
        self.last_request = ""
        self.opening = ""
        self.numbers: set = set()
        self.open_todos: list = []
        self.remaining_lines: list = []
        self.closing_question = ""

    @property
    def score(self) -> int:
        # The sum of the signal weights.
        return sum(s.weight for s in self.signals)

    @property
    def candidate(self) -> bool:
        # True when the session looks unfinished enough to be checked further.
        return self.score >= CANDIDATE_SCORE

    @property
    def strong(self) -> bool:
        # True when the evidence is strong (several signals, or one heavy one plus another).
        return self.score >= STRONG_SCORE

    def signal_ids(self) -> list:
        # The ids of the signals, in detection order.
        return [s.id for s in self.signals]

    def why(self) -> str:
        # The signal labels joined into one plain-English clause.
        return "; ".join(s.label for s in self.signals)


class TaskText:
    """Turns prompts, titles, todos, and actions into comparable keyword and file sets."""

    @staticmethod
    def keywords(*texts: str) -> set:
        # Lowercase words of 3+ characters and identifiers, minus stopwords; issue numbers like #12 are kept.
        found = set()
        for text in texts:
            for token in WORD_RE.findall(str(text or "")):
                word = token.lower().strip("-_.")
                if word.startswith("#"):
                    found.add(word)
                    continue
                if len(word) < 3 or word in STOPWORDS or word.isdigit():
                    continue
                found.add(word)
                stem = word.split(".", 1)[0]
                if stem != word and len(stem) >= 3 and stem not in STOPWORDS:
                    found.add(stem)
        return found

    @staticmethod
    def touched_files(turns: list) -> set:
        # Base names of the files the session edited, from "Edit <path>" actions.
        names = set()
        for turn in turns:
            for action in turn.actions:
                if action.startswith(EDIT_PREFIX):
                    path = action[len(EDIT_PREFIX):].strip().strip("'\"")
                    base = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
                    if base and base != "(file)":
                        names.add(base)
        return names

    @staticmethod
    def opening(text: str) -> str:
        # The whole first request, lowercased with spaces collapsed, used to recognize a resumed or forked copy.
        return " ".join(str(text or "").lower().split())

    @staticmethod
    def numbers(*texts: str) -> set:
        # Pull request and issue numbers named in the texts ("#12", "PR 12", ".../pull/12"), minus colors like #333.
        found = set()
        for text in texts:
            for groups in TASK_NUMBER_RE.findall(str(text or "")):
                number = next((g for g in groups if g), "")
                if number and not (len(number) in (3, 6) and len(set(number)) == 1):
                    found.add(number.lstrip("0") or "0")
        return found


class PastedText:
    """Recognizes instruction text pasted into many sessions' prompts (skills, templates) and removes it."""

    def __init__(self, sessions: list) -> None:
        # Counts, per line of prompt text, how many sessions contain it; lines in 3+ sessions are pasted text.
        seen = {}
        for session in sessions:
            lines = set()
            for turn in session.turns:
                lines.update(self._lines(turn.text))
            for line in lines:
                seen[line] = seen.get(line, 0) + 1
        self.repeated = {line for line, count in seen.items() if count >= PASTE_MIN_SESSIONS}

    def strip(self, text: str) -> str:
        # The text without pasted lines when 4+ of its lines are pasted; otherwise the text unchanged.
        raw = str(text or "")
        lines = raw.splitlines()
        pasted = [self._norm(line) in self.repeated for line in lines]
        if sum(pasted) < PASTE_MIN_LINES:
            return raw
        return "\n".join(line for i, line in enumerate(lines) if not pasted[i]).strip()

    @classmethod
    def _lines(cls, text: str) -> set:
        # The normalized lines of one prompt that are long enough to count.
        found = set()
        for line in str(text or "")[:PASTE_SCAN_CHARS].splitlines():
            norm = cls._norm(line)
            if norm:
                found.add(norm)
        return found

    @staticmethod
    def _norm(line: str) -> str:
        # The line lowercased with spaces collapsed, or "" when it is too short to identify pasted text.
        norm = " ".join(line.lower().split())
        return norm if len(norm) >= PASTE_LINE_CHARS else ""


class UnfinishedDetector:
    """Runs every unfinished-work check over a list of sessions."""

    def __init__(self, now=None) -> None:
        # now is the reference time for "recent" checks (aware UTC); None means the real clock.
        self.now = now
        self._turn_end_harnesses: set = set()
        self._pasted = PastedText([])

    def detect(self, sessions: list) -> list:
        # Returns one Finding per session, in the sessions' order.
        self._turn_end_harnesses = self.harnesses_with_turn_ends(sessions)
        self._pasted = PastedText(sessions)
        return [self.inspect(session) for session in sessions]

    def harnesses_with_turn_ends(self, sessions: list) -> set:
        # Harnesses where at least one session recorded a finished turn, so "working" really means mid-turn.
        found = set()
        for session in sessions:
            if session.end_state in ("finished", "question") or any(t.after_finished for t in session.turns[1:]):
                found.add(session.harness)
        return found

    def inspect(self, session) -> Finding:
        # Builds the finding for one session: signals first, then the facts used to match and verify it.
        finding = Finding(session)
        if not session.turns or self._is_trivial(session):
            return finding
        self._end_state_signals(session, finding)
        self._todo_signal(session, finding)
        self._remaining_work_signal(session, finding)
        self._last_turn_signals(session, finding)
        self._collect_facts(session, finding)
        return finding

    def _is_trivial(self, session) -> bool:
        # A single short prompt ("hi", "test"), or only short prompts with no tool call, is never a piece of work.
        short = [TextTools.word_count(turn.text) < 4 for turn in session.turns]
        if len(short) == 1 and short[0]:
            return True
        return all(short) and not any(turn.actions or turn.tool_calls for turn in session.turns)

    def _end_state_signals(self, session, finding: Finding) -> None:
        # Adds the signal for how the last turn ended: mid-turn, interrupted, error, or a question.
        state = session.end_state
        if state == "working" and session.harness in self._turn_end_harnesses:
            finding.signals.append(Signal("stopped_mid_turn", 3, "stopped in the middle of a turn, with no final answer"))
        elif state == "interrupted":
            finding.signals.append(Signal("interrupted", 3, "the user interrupted the agent's last turn and did not come back"))
        elif state == "error":
            finding.signals.append(Signal("ended_in_error", 3, "the last turn ended with an error"))
        elif state == "question":
            question = self._closing_question(session.final_agent_text)
            finding.closing_question = question
            if question and OFFER_RE.search(question):
                finding.signals.append(Signal("offered_follow_up", 1, "the agent offered more work and nobody answered", question))
            else:
                finding.signals.append(Signal("unanswered_question", 2, "the agent's last message asked a question nobody answered", question))

    def _todo_signal(self, session, finding: Finding) -> None:
        # Adds open_todos when the last todo snapshot still has pending or in-progress items.
        open_items = [(content, status) for content, status in session.todos if str(status).lower() not in DONE_TODO_STATUSES]
        if not open_items:
            return
        finding.open_todos = open_items[:MAX_TODOS_SHOWN]
        label = "%d of %d todo items still open" % (len(open_items), len(session.todos))
        finding.signals.append(Signal("open_todos", 3, label, "; ".join(c for c, _ in open_items[:3])))

    def _remaining_work_signal(self, session, finding: Finding) -> None:
        # Adds remaining_work when the final agent text names work still to do, ignoring negations.
        lines = []
        for sentence in SENTENCE_RE.split(session.final_agent_text or ""):
            sentence = sentence.strip()
            if not sentence or NEGATED_RE.search(sentence):
                continue
            if REMAINING_RE.search(sentence) or TODO_WORD_RE.search(sentence):
                lines.append(TextTools.one_line(sentence, REMAINING_LINE_CHARS))
            if len(lines) >= MAX_REMAINING_LINES:
                break
        if lines:
            finding.remaining_lines = lines
            finding.signals.append(Signal("remaining_work", 2, "the final message names work still to do", lines[0]))

    def _last_turn_signals(self, session, finding: Finding) -> None:
        # Adds no_reply for an unanswered last request and failed_at_end for failing tool calls at the end.
        last = session.turns[-1]
        if not last.reply_text.strip() and not last.interrupted and session.end_state not in ("interrupted", "error"):
            finding.signals.append(Signal("no_reply", 2, "the user's last request got no reply"))
        if last.failed_actions > 0:
            heavy = last.failed_actions >= 3 or session.end_state not in ("finished", "question")
            label = "%d tool call%s failed in the last turn" % (last.failed_actions, "" if last.failed_actions == 1 else "s")
            finding.signals.append(Signal("failed_at_end", 2 if heavy else 1, label))

    def _collect_facts(self, session, finding: Finding) -> None:
        # Gathers the user's own request text, task keywords and numbers, touched files, branches, and pull requests.
        todo_text = " ".join(content for content, _ in session.todos)
        finding.first_request = self._pasted.strip(session.turns[0].text)
        finding.last_request = self._pasted.strip(session.turns[-1].text) if len(session.turns) > 1 else finding.first_request
        first, last = finding.first_request, finding.last_request
        finding.opening = TaskText.opening(first)
        finding.keywords = TaskText.keywords(session.title, first[:2000], last[:1000], todo_text)
        finding.numbers = TaskText.numbers(session.title, first[:2000], last[:1000])
        finding.files = TaskText.touched_files(session.turns)
        finding.branches = self._branches(session.turns)
        finding.pr_urls, finding.mentioned_pr_urls = self._pr_urls(session, finding.numbers)

    @staticmethod
    def _closing_question(text: str) -> str:
        # The last sentence of text that ends with a question mark, or "".
        sentences = [s.strip() for s in SENTENCE_RE.split(text or "") if s.strip()]
        for sentence in reversed(sentences):
            if sentence.endswith("?"):
                return TextTools.one_line(sentence, REMAINING_LINE_CHARS)
        return ""

    @staticmethod
    def _branches(turns: list) -> list:
        # Branch names the session created or pushed, in first-seen order, without default branches.
        names = []
        for turn in turns:
            for action in turn.actions:
                match = SHELL_ACTION_RE.match(action)
                if not match:
                    continue
                for pattern in BRANCH_RES:
                    for name in pattern.findall(match.group(1)):
                        name = name.strip("'\"")
                        if name and name.lower() not in DEFAULT_BRANCH_NAMES and name not in names:
                            names.append(name)
        return names[-5:]

    @staticmethod
    def _pr_urls(session, task_numbers: set) -> tuple:
        # (own, mentioned): pull requests the session created or was asked about, and ones it only mentioned; 3 each.
        own, mentioned = [], []
        texts = [(turn.reply_text, any(PR_CREATE_RE.search(a) for a in turn.actions), turn.actions) for turn in session.turns]
        texts.append((session.final_agent_text, False, []))
        for reply, created, actions in texts:
            for text in [reply] + list(actions):
                for match in PR_URL_RE.finditer(text or ""):
                    url = match.group(0)
                    is_own = created or match.group(1) in task_numbers or url in own
                    for bucket in (own, mentioned):
                        if url in bucket:
                            bucket.remove(url)
                    (own if is_own else mentioned).append(url)
        return own[-3:], mentioned[-3:]


class FolderNames:
    """Path helpers shared by the matcher, the probe, and the digest."""

    @staticmethod
    def is_real(path: str) -> bool:
        # True for an absolute folder path (Gemini can report "gemini project <hash>" instead of a path).
        return bool(path) and (os.path.isabs(path) or bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("/"))

    @staticmethod
    def norm(path: str) -> str:
        # Forward slashes, no trailing slash, and case-folded on Windows, for comparison only.
        value = str(path or "").replace("\\", "/").rstrip("/")
        if os.name == "nt" or re.match(r"^[A-Za-z]:/", value):
            value = value.lower()
        return value

    @staticmethod
    def inside(path: str, folder: str) -> bool:
        # True when path is folder or below it; /repo is not inside /repo2.
        a, b = FolderNames.norm(path), FolderNames.norm(folder)
        return bool(a and b) and (a == b or a.startswith(b + "/"))

    @staticmethod
    def base(path: str) -> str:
        # The last component of a folder path, or "".
        return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
