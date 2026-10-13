"""Finds the prompts where the user corrected what their coding agent had just done.

A correction at turn t is about turn t-1: sessionkit stores on each Turn what the agent did after
that prompt (its tool actions, failed calls, and whether the user interrupted it). The correction
text is turn t's prompt, and turn t's own actions show what the agent did after being corrected.
Consecutive corrections in one session form one Episode: one mistake that cost several prompts.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"
LEVEL = {STRONG: 3, MEDIUM: 2, WEAK: 1, "": 0}
BY_LEVEL = {3: STRONG, 2: MEDIUM, 1: WEAK, 0: ""}

SIGNAL_LABELS = {
    "pushback": "pushed back on what the agent did",
    "undo": "asked to undo or revert",
    "restated": "had to repeat an earlier instruction",
    "frustration": "showed frustration",
    "directive": "told the agent to stop or not do something",
    "still_broken": "said it still did not work",
    "instead": "asked for something else instead",
    "interrupt_redirect": "interrupted the agent, then redirected it",
    "fix_after_failure": "supplied the fix after the agent's tool calls failed",
    "hesitation": "hesitated (actually, wait)",
}


class CorrectionRules:
    """The phrase tables that decide whether a prompt corrects the agent's previous turn."""

    LEAD_CHARS = 400
    SIGNAL_CHARS = 200
    FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
    INLINE_CODE_RE = re.compile(r"`[^`\n]{0,300}`")
    QUOTE_LINE_RE = re.compile(r"(?m)^[ \t]*>.*$")

    PATTERNS = {
        "pushback": re.compile(
            r"^(?:no|nope|nah)\b[\s,.!:;-]+\S"
            r"|\b(?:that|this|it)(?:'s| is| was)\s+(?:not right|not correct|not what|wrong|incorrect)\b"
            r"|\byou(?:'re| are)\s+(?:wrong|not listening|doing it wrong)\b"
            r"|\bwrong (?:file|folder|directory|dir|branch|repo|repository|place|path|one|approach|way|function|method|class"
            r"|component|command|version|port|table|column|endpoint|url|model|project|worktree|account|env|environment)\b"
            r"|\bi (?:meant|said|asked (?:you|for)|told you|did ?n'?t (?:ask|want|say|tell))\b"
            r"|\byou (?:did ?n'?t|did not|forgot|missed|broke|deleted|removed|overwrote|ignored|skipped|never|have ?n'?t|still have ?n'?t)\b"
            r"|\bwhy (?:did|would|are) you\b"
            r"|\bnot what i\b|\bnot (?:that|this) one\b|\bthe other (?:one|file|branch|repo|function)\b"
        ),
        "undo": re.compile(
            r"\b(?:undo|revert|roll ?back|put (?:it|that|them|this) back|change (?:it|that|this) back"
            r"|restore (?:it|that|this|the original|the previous|what)|go back to (?:the|what|how) (?:it|original|previous|was))\b"
        ),
        "restated": re.compile(r"\b(?:i already (?:told|said|asked)|as i (?:said|mentioned)|like i said|i just said|how many times|for the (?:second|third|last) time|i repeat)\b"),
        "frustration": re.compile(r"\b(?:wtf|what the (?:hell|heck|fuck)|what are you doing|seriously\?|are you kidding|come on[,!.]|ffs)"),
        "directive": re.compile(
            r"^stop(?=[\s,.!:;-]*$|[,.!:;-]|\s+(?:it|that|this|now|right|there|please|immediately|doing|what you|you)\b|\s+\w+ing\b)"
            r"|\b(?:stop|don'?t|do not|never|quit) (?:(?!(?:the|a|an|this|that|these|those|my|your|all|any) )\w+ )?"
            r"(?:add|chang|touch|us|creat|delet|run|commit|push|modif|edit|writ"
            r"|rewrit|remov|install|refactor|renam|mak|break|guess|assum|mock|skip|ignor|overwrit|reformat)\w*"
        ),
        "still_broken": re.compile(
            r"\b(?:still (?:not|broken|failing|fails|failed|wrong|the same|there|getting|seeing|happening|does ?n'?t)|does ?n'?t work"
            r"|did ?n'?t work|is ?n'?t working|not working|(?:it|that|you|this) broke|broke (?:it|the|my)|not fixed|same (?:error|issue|problem|bug))\b"
        ),
        "instead": re.compile(r"\b(?:instead|rather than|should(?: not|n'?t)? have|supposed to)\b"),
        "hesitation": re.compile(r"^(?:actually|wait|hmm+|hold on|hang on)\b"),
    }
    LEVELS = {"pushback": STRONG, "undo": STRONG, "restated": STRONG, "frustration": STRONG, "directive": MEDIUM,
              "still_broken": MEDIUM, "instead": MEDIUM, "interrupt_redirect": MEDIUM, "fix_after_failure": MEDIUM, "hesitation": WEAK}
    BARE_NO_RE = re.compile(r"^(?:no|nope|nah)[.!]*$")
    DECLINE_RE = re.compile(r"^(?:no|nope|nah)(?:,? thanks?(?: you)?)?[.!]*$")
    NOT_CORRECTION_RE = re.compile(r"^(?:no (?:problem|worries|rush|need to worry)|not bad|nothing else)\b")
    SELF_FIX_RE = re.compile(r"^(?:\S+\s){0,3}\S+\*$|^\*\S+(?:\s\S+){0,3}$|^\S+(?:\s\S+)?,? i mean[.!]*$")
    RESUME_RE = re.compile(r"^(?:continue|go on|keep going|resume|carry on|proceed|go ahead|ok(?:ay)?|yes|yep|sure)\b[\s.!]*(?:please)?[\s.!]*$")
    NUDGE_RE = re.compile(r"^(?:please )?(?:try again|retry|fix (?:it|that|this|them|the errors?|these errors?|those errors?)|keep going|continue|go on)\b")
    FIX_CUE_RE = re.compile(
        r"`[^`]+`"
        r"|\b(?:npm|pnpm|yarn|pip3?|uv|python3?|py|pytest|git|gh|docker|make|cargo|go|node|npx|bun|poetry|conda|brew|kubectl|terraform)\s+[\w-]"
        r"|\b(?:use|try|run|you need to|need to|the (?:issue|problem|error|fix|reason) is|it'?s because|because the|should be"
        r"|it'?s (?:in|under|at)|look (?:in|at|under)|activate|install|set|export|the (?:file|path|folder|command|port|env|venv) is)\b"
        r"|[\w.-]+/[\w.-]+\.\w+|[\w.-]+/[\w.-]+/|[a-z]:\\"
    )
    COMMAND_RE = re.compile(r"^(?:/|\$[a-z][\w-]*)")
    TEMPLATE_START_RE = re.compile(r"^\s*(?:#{1,6} \S|<[a-z][\w-]*>|(?:\d+\.|[-*])\s+\*\*\S)")
    PRAISE_RE = re.compile(r"^(?:great|perfect|nice|awesome|amazing|excellent|sweet|cool|good|thanks|thank you|lgtm|looks good|love it|ok(?:ay)?,? (?:great|perfect|nice|cool|good|thanks))\b")
    ADDITIVE_RE = re.compile(r"\b(?:also|as well|in addition|btw|by the way)\b|\btoo[.!]*$|\b(?:after|when|once) (?:you(?:'re| are)? )?(?:done|finished|finish)\b")
    QUESTION_START_RE = re.compile(r"^(?:and |so |but |ok(?:ay)?,? )?(?:what|which|who|where|when|how|does|do|did|is|are|was|were|explain|tell me|walk me|describe|show me|can you explain)\b")
    FIX_MAX_WORDS = 40
    NEEDS_ACTION = {"undo", "directive"}
    SENTENCE_END_RE = re.compile(r"[.!?;]\s")
    TEMPLATE_WORDS = 25

    def normalize(self, text: str) -> str:
        # Removes code, quoted lines, and curly apostrophes; returns the lower-cased lead of the prompt.
        value = str(text or "").replace("’", "'").replace("‘", "'")
        value = self.FENCE_RE.sub(" ", value)
        value = self.QUOTE_LINE_RE.sub(" ", value)
        value = self.INLINE_CODE_RE.sub(" code ", value)
        return " ".join(value.split()).lower()[: self.LEAD_CHARS]

    def raw_lead(self, text: str) -> str:
        # The lower-cased start of the raw prompt, inline code kept, for fix cues such as `pnpm install`.
        value = str(text or "").replace("’", "'")
        value = self.FENCE_RE.sub(" ", value)
        return " ".join(value.split()).lower()[: self.LEAD_CHARS]

    def template_key(self, text: str) -> str:
        # The whole prompt, collapsed and lower-cased, when it is long enough to be a pasted or expanded template; else "".
        value = " ".join(str(text or "").split()).lower()
        return value if len(value.split()) >= self.TEMPLATE_WORDS else ""

    def signals(self, text: str, turn, previous) -> tuple:
        # Returns (signal names, strength) for turn's prompt against the previous turn, or ((), "") when it is no correction.
        lead = self.normalize(text)
        if not lead or self.COMMAND_RE.match(lead) or self.TEMPLATE_START_RE.match(str(text or "")) or self.NOT_CORRECTION_RE.match(lead) or self.SELF_FIX_RE.match(lead):
            # A user fixing a typo in their own last prompt ("next pr*", "516 i mean") is not correcting the agent.
            return (), ""
        head = lead[: self.SIGNAL_CHARS]
        acted = previous.tool_calls > 0
        found = [name for name, pattern in self.PATTERNS.items() if pattern.search(head) and (acted or previous.interrupted or name not in self.NEEDS_ACTION)]
        if "instead" in found and not self.PATTERNS["instead"].search(head[:120]):
            found.remove("instead")
        if found == ["directive"] and not self.PATTERNS["directive"].search(self.SENTENCE_END_RE.split(head, 1)[0]):
            found.remove("directive")  # "Add the endpoint. Don't touch the schema." is a new task with a constraint.
        if self.BARE_NO_RE.match(lead) and acted and not turn.after_question:
            found.append("pushback")
        if previous.interrupted and not self.RESUME_RE.match(lead) and len(lead.split()) >= 2 and not self.ADDITIVE_RE.search(head) and not self.is_question(lead):
            found.append("interrupt_redirect")
        if self._supplies_fix(text, lead, previous):
            found.append("fix_after_failure")
        return self._grade(found, lead, turn, previous)

    def is_question(self, lead: str) -> bool:
        # True for a short question such as "and which ones do we already have": a clarification, not a redirect.
        return len(lead.split()) <= 25 and bool(self.QUESTION_START_RE.match(lead) or lead.rstrip().endswith("?"))

    def _supplies_fix(self, text: str, lead: str, previous) -> bool:
        # True when the previous turn had failed tool calls and this short prompt carries a concrete fix.
        if previous.failed_actions < 1 or len(str(text or "").split()) > self.FIX_MAX_WORDS:
            return False
        if previous.failed_actions * 4 < previous.tool_calls and previous.failed_actions < 3:
            return False  # One or two failed calls in a long turn are normal work the agent recovered from.
        if self.QUESTION_START_RE.match(lead) or (self.NUDGE_RE.match(lead) and len(lead.split()) <= 6):
            return False
        return bool(self.FIX_CUE_RE.search(self.raw_lead(text)[: self.SIGNAL_CHARS]))

    def _grade(self, found: list, lead: str, turn, previous) -> tuple:
        # Picks the strongest signal level, downgrades it after an agent question, and drops weak-only prompts.
        names = tuple(dict.fromkeys(found))
        if not names:
            return (), ""
        level = max(LEVEL[self.LEVELS[name]] for name in names)
        if level < LEVEL[STRONG] and self.PRAISE_RE.match(lead):
            return (), ""  # "great, now merge it" approves the last turn and starts the next task.
        if turn.after_question:
            if self.DECLINE_RE.match(lead):
                return (), ""
            level -= 1
        if level < LEVEL[MEDIUM]:
            return (), ""
        return names, BY_LEVEL[level]


class Correction:
    """One prompt that corrects the agent's previous turn."""

    __slots__ = ("session", "turn", "previous", "signals", "strength", "after")

    def __init__(self, session, turn, previous, signals: tuple, strength: str) -> None:
        # Pairs the correcting turn with the corrected one (whose actions hold what the agent did).
        self.session = session
        self.turn = turn
        self.previous = previous
        self.signals = signals
        self.strength = strength
        self.after = list(turn.actions[:3])

    @property
    def text(self) -> str:
        # The raw correction prompt (unredacted).
        return self.turn.text


class Episode:
    """Consecutive corrections in one session: one mistake, however many prompts it took to fix."""

    def __init__(self, session, ask_turn, corrections: list) -> None:
        # ask_turn is the prompt the agent was answering when it made the mistake (may equal a correction's previous).
        self.session = session
        self.ask_turn = ask_turn
        self.corrections = corrections
        self.kind = "other"

    @property
    def first(self) -> Correction:
        # The episode's first correction.
        return self.corrections[0]

    @property
    def prompts_lost(self) -> int:
        # Prompts the user spent correcting this mistake.
        return len(self.corrections)

    @property
    def tool_calls_redone(self) -> int:
        # Tool calls the agent made in the turns the user then corrected.
        return sum(c.previous.tool_calls for c in self.corrections)

    @property
    def failed_actions(self) -> int:
        # Failed tool calls in the corrected turns.
        return sum(c.previous.failed_actions for c in self.corrections)

    @property
    def interrupted(self) -> bool:
        # True when the user interrupted any corrected turn.
        return any(c.previous.interrupted for c in self.corrections)

    @property
    def strong(self) -> int:
        # Number of strong corrections in the episode.
        return sum(1 for c in self.corrections if c.strength == STRONG)

    @property
    def signals(self) -> Counter:
        # Signal counts over every correction in the episode.
        return Counter(name for c in self.corrections for name in c.signals)

    @property
    def last_ts(self):
        # Time of the newest correction, or the session's last activity.
        stamps = [c.turn.ts for c in self.corrections if c.turn.ts is not None]
        return max(stamps) if stamps else self.session.last_ts

    @property
    def first_ts(self):
        # Time of the first correction, or the session's first activity.
        stamps = [c.turn.ts for c in self.corrections if c.turn.ts is not None]
        return min(stamps) if stamps else self.session.first_ts

    def correction_text(self, limit: int = 1500) -> str:
        # The episode's correction prompts joined, lower-cased, for kind rules and term features.
        return " \n ".join(CorrectionRules().normalize(c.text) for c in self.corrections)[:limit]

    def corrected_actions(self) -> list:
        # Every action of the corrected turns, in order.
        return [a for c in self.corrections for a in c.previous.actions]


class CorrectionDetector:
    """Walks every session's turns and returns the correction episodes."""

    TEMPLATE_SESSIONS = 2
    TEMPLATE_TURNS = 3

    def __init__(self, rules: CorrectionRules = None) -> None:
        # Uses the default phrase tables unless a test passes its own.
        self.rules = rules or CorrectionRules()
        self.templates: set = set()
        self.templates_skipped = 0

    def detect(self, sessions: list) -> list:
        # Returns every Episode across sessions, newest first, after setting aside repeated long templates.
        self.templates = self.find_templates(sessions)
        self.templates_skipped = 0
        episodes = []
        for session in sessions:
            episodes.extend(self.session_episodes(session))
        floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return sorted(episodes, key=lambda e: e.last_ts or floor, reverse=True)

    def find_templates(self, sessions: list) -> set:
        # Long prompts seen in 2+ sessions or 3+ times: slash-command templates (OpenCode expands them inline) and reused pastes.
        turns_seen, sessions_seen = Counter(), {}
        for session in sessions:
            for turn in session.turns:
                key = self.rules.template_key(turn.text)
                if key:
                    turns_seen[key] += 1
                    sessions_seen.setdefault(key, set()).add(id(session))
        return {k for k, n in turns_seen.items() if n >= self.TEMPLATE_TURNS or len(sessions_seen[k]) >= self.TEMPLATE_SESSIONS}

    def session_episodes(self, session) -> list:
        # Finds the corrections of one session and groups consecutive ones into episodes.
        corrections = self.session_corrections(session)
        episodes, run = [], []
        for correction in corrections:
            if run and correction.turn.index != run[-1].turn.index + 1:
                episodes.append(self._episode(session, run))
                run = []
            run.append(correction)
        if run:
            episodes.append(self._episode(session, run))
        return episodes

    def session_corrections(self, session) -> list:
        # Checks every turn after the first against the turn before it.
        found = []
        turns = session.turns
        for position in range(1, len(turns)):
            turn, previous = turns[position], turns[position - 1]
            if self.templates and self.rules.template_key(turn.text) in self.templates:
                self.templates_skipped += 1
                continue
            names, strength = self.rules.signals(turn.text, turn, previous)
            if names:
                found.append(Correction(session, turn, previous, names, strength))
        return found

    def overall(self, sessions: list, episodes: list) -> dict:
        # Totals for the digest header: corrections, episodes, rates, and counts by signal and strength.
        prompts = sum(s.prompt_count for s in sessions)
        corrections = [c for e in episodes for c in e.corrections]
        by_harness = Counter(s.harness for s in sessions)
        return {
            "sessions": len(sessions),
            "prompts": prompts,
            "by_harness": dict(by_harness),
            "corrections": len(corrections),
            "episodes": len(episodes),
            "sessions_with_corrections": len({id(e.session) for e in episodes}),
            "per_100_prompts": round(100.0 * len(corrections) / prompts, 1) if prompts else 0.0,
            "prompts_lost": sum(e.prompts_lost for e in episodes),
            "tool_calls_redone": sum(e.tool_calls_redone for e in episodes),
            "by_signal": dict(Counter(name for c in corrections for name in c.signals).most_common()),
            "by_strength": dict(Counter(c.strength for c in corrections)),
            "templates_skipped": self.templates_skipped,
        }

    def _episode(self, session, run: list) -> Episode:
        # Builds an Episode whose ask turn is the task before its first correction, looking past bare "continue" prompts.
        return Episode(session, self.ask_turn(session, run[0].previous), list(run))

    def ask_turn(self, session, previous):
        # The last prompt at or before previous that is not a bare resume such as "continue", at most 5 turns back.
        turns = session.turns
        position = next((i for i, t in enumerate(turns) if t is previous), None)
        if position is None:
            return previous
        for back in range(position, max(position - 5, -1), -1):
            if not self.rules.RESUME_RE.match(self.rules.normalize(turns[back].text)):
                return turns[back]
        return previous
