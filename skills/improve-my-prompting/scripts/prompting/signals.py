"""Keyword and shape heuristics that find prompting habits in a user's sessions."""

from __future__ import annotations

import re
from collections import Counter, OrderedDict

from sessionkit.model import TextTools

LEAD_CHARS = 100  # Corrections live in the first words; a pasted log further down must not count.
SCAN_CHARS = 400
CLEAN_CHARS = 4000
SHORT_OPENER_WORDS = 12
GO_AHEAD_WORDS = 8
LONG_SESSION_PROMPTS = 30
HUGE_PASTE_CHARS = 8000
DRIP_MIN_PROMPTS = 3
CLEAR_ASK_WORDS = 25
NEGATIVE_MAX_WORDS = 300  # Long documents naturally hold many "don't"s; the habit is a short prompt made of them.
TEMPLATE_MIN_USES = 3
TEMPLATE_MIN_CHARS = 1000
TEMPLATE_SENSITIVE = ("persona", "emphasis", "negative_only", "huge_paste", "step_by_step", "emotional_stakes")


class Patterns:
    """The regular expressions behind every signal, kept in one place so they are easy to audit."""

    SENTENCE_START = r"(?:^|[.!?\n]\s*)(?:(?:ok(?:ay)?|hmm+|um+|so|but|wait|hey)[,.]?\s+)?"
    CORRECTION = re.compile(SENTENCE_START + r"(?:"
        r"no(?:pe)?\b(?![ -]+(?:problem|worries|need|thanks|rush|longer|one|matter|idea|more|other|changes?|errors?|issues?))"
        r"|not (?:that|this|what i)\b|that'?s not\b|that is not\b|this is not (?:what|right)|this isn'?t (?:what|right)"
        r"|wrong\b|(?:that'?s|this is|it'?s|you'?re|you are) (?:still )?wrong\b|undo\b|revert\b|roll ?back\b"
        r"|i meant\b|i said\b|why did you\b|why would you\b|you didn'?t\b|you did not\b|you forgot\b|you missed\b|you broke\b"
        r"|stop(?:[,.!]|\s+(?:doing|it|that|this|changing|editing|adding|using|touching|making|trying|deleting|removing)\b)|actually,|instead,|instead of that\b|don'?t do that\b|still (?:not|broken|wrong|failing|the same)\b|it'?s still\b"
        r")", re.I)
    CONFIRMATION = re.compile(
        r"^(?:y|ya|yes|yep|yeah|yup|sure|ok|okay|k|go|go ahead|go for it|do it|do that|proceed|continue|sounds good|"
        r"lgtm|looks good|please do|yes please|ship it|approved|approve|confirm(?:ed)?|correct|right|perfect|great|"
        r"option \w+|\d|[a-d])\b", re.I)
    PERMISSION_ASK = re.compile(r"(?:want me to|should i|shall i|would you like me to|do you want me to|let me know if you(?:'d)? (?:want|like))", re.I)
    PATH_OR_CODE = re.compile(r"(?:[\\/][\w.-]+[\\/]|\b[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|rb|java|kt|cs|cpp|c|h|md|json|ya?ml|toml|sql|sh|ps1|css|html|vue|svelte)\b|`[^`]+`|https?://|\b\w+\(\)|::|#\d+|\b[a-f0-9]{7,40}\b)", re.I)
    NO_REFERENT = re.compile(
        r"^(?:fix (?:this|it|that)\b|why is (?:it|this|that) (?:failing|broken|not working)|continue\b|keep going\b|"
        r"try again\b|it'?s (?:broken|not working)|(?:it|this|that) (?:doesn'?t|does not) work|same (?:error|issue|problem)|"
        r"look at this\b|check this\b|what happened\b|any updates?\b)", re.I)
    DRIP = re.compile(r"^(?:also\b|and also\b|one more(?: thing)?\b|oh,? and\b|oh,? also\b|additionally\b|btw\b|by the way\b|forgot to mention|i forgot\b|make sure (?:it|you) also\b|another thing\b|plus,)", re.I)
    RESTATED = re.compile(r"(?:i already (?:told|said|asked|mentioned)|as i (?:said|mentioned)|like i said|i just said|how many times|for the (?:second|third|fourth|last) time|i told you|i repeat\b|once again,|(?:^|[.!?]\s+)again,)", re.I)
    FRUSTRATION = re.compile(r"(?:\bwtf\b|what the (?:hell|fuck|heck)|are you (?:serious|kidding)|\bseriously\?|\buseless\b|\bcome on\b|\bffs\b|\bugh+\b|\byou(?:'re| are)? (?:an )?(?:idiot|stupid|dumb)\b)", re.I)
    STAKES = re.compile(r"(?:\btip you\b|\$\d+ tip|i'?ll tip\b|you'?ll be (?:fired|punished|replaced|shut down)|my job depends|i'?ll lose my job|\bor else\b|please,? please|i'?m begging|i beg you|lives depend|someone will die)", re.I)
    PERSONA = re.compile(r"\b(?:you are|you're|act as|pretend (?:to be|you are)|imagine you are|behave like)\s+(?:an?\s+|the\s+)?(?:world[- ]class|expert|senior|10x|genius|elite|principal|staff|top|seasoned|veteran|brilliant|legendary)", re.I)
    STEP_BY_STEP = re.compile(r"(?:think step[- ]by[- ]step|step-by-step reasoning|let'?s think\b|take a deep breath|reason step by step)", re.I)
    NEGATIVE = re.compile(r"\b(?:don'?t|do not|never|no more|stop)\b", re.I)
    NEGATIVE_ESCAPE = re.compile(r"\b(?:instead|rather)\b", re.I)
    CAPS_WORD = re.compile(r"(?<![\w./\\-])[A-Z]{4,}(?![\w./\\-])")
    CAPS_DIRECTIVE = re.compile(r"(?<![\w./\\-])(?:MUST|NEVER|ALWAYS|CRITICAL|IMPORTANT|DO NOT|DON'T)(?![\w./\\-])")
    BANGS = re.compile(r"!!!")
    FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
    INLINE_CODE = re.compile(r"`[^`\n]*`")
    QUOTED_LINE = re.compile(r"(?m)^\s*>.*$")
    PLACEHOLDER = re.compile(r"<[^<>\n]{1,120}>|\{[^{}\n]{1,120}\}|\"[^\"\n]{1,200}\"")
    REASON = re.compile(r"\b(?:because|so that|so i can|so we can|the goal is|in order to|the reason)\b", re.I)
    CHECK = re.compile(r"\b(?:tests?|verify|make sure .{0,40}pass|done when|until\b|confirm|should pass|must pass|run (?:the )?(?:build|lint|tests?))\b", re.I)
    ACRONYMS = {
        "TODO", "README", "JSON", "HTTP", "HTTPS", "YAML", "TOML", "UUID", "OAUTH", "REST", "GRPC", "CORS", "CRUD", "ASCII",
        "BASH", "AGENTS", "CLAUDE", "GEMINI", "NULL", "TRUE", "FALSE", "NOTE", "WARN", "INFO", "DEBUG", "ERROR", "FATAL",
        "POST", "PATCH", "DELETE", "HEAD", "PATH", "HOME", "UTF8", "LICENSE", "CHANGELOG", "CONTRIBUTING", "MAKEFILE",
        "DOCKERFILE", "OPENAI", "NEXT", "NODE", "VITE", "HTML", "WASM", "ARIA", "SAML", "LDAP", "SMTP", "IMAP", "AJAX",
        "NOSQL", "MYSQL", "REDIS", "MONGO", "AUTH", "SKILL", "EDIT", "WRITE", "READ", "GREP", "GLOB", "TASK", "PROMPT",
        "MEMORY", "PLAN", "SPEC", "NOTES", "DONE", "WIP", "XXX", "FIXME", "HACK", "ENOENT", "EACCES", "EPERM", "SIGINT",
        "SIGTERM", "STDOUT", "STDERR", "STDIN", "CSRF", "XSS", "SSRF", "IPV4", "IPV6", "UTC", "ISO", "GMT", "CUDA", "NVIDIA",
        "MCP", "LLM", "LLMS", "APIS", "URLS", "CLIS", "SDKS", "PRS", "IDS", "UUIDS", "JWTS",
    }

    @staticmethod
    def clean(text: str) -> str:
        # Removes code fences, inline code, and quoted lines, so pasted errors in capitals are not shouting.
        value = Patterns.FENCE.sub(" ", text[:CLEAN_CHARS * 2])
        value = Patterns.INLINE_CODE.sub(" ", value)
        value = Patterns.PLACEHOLDER.sub(" ", value)
        return Patterns.QUOTED_LINE.sub(" ", value)[:CLEAN_CHARS]


class SignalDefinition:
    """One measurable habit: its id, title, rubric IDs, how it is measured, and its scope."""

    def __init__(self, signal_id: str, title: str, rubric: tuple, measure: str, scope: str = "turn") -> None:
        # scope is "turn" (one prompt) or "session" (a pattern across a session's prompts).
        self.id = signal_id
        self.title = title
        self.rubric = rubric
        self.measure = measure
        self.scope = scope


class SignalHit:
    """One occurrence of a signal: the session, the prompt (None for session scope), and context."""

    __slots__ = ("session", "turn", "context")

    def __init__(self, session, turn, context: dict = None) -> None:
        # context may hold prev_text (the earlier prompt) and turns (the prompts of a session-scope hit).
        self.session = session
        self.turn = turn
        self.context = context or {}

    def when(self):
        # The time used to rank examples by recency: the prompt's time, else the session's last activity.
        stamp = self.turn.ts if self.turn is not None and self.turn.ts else self.session.last_ts
        return stamp.timestamp() if stamp else 0.0


class TemplateIndex:
    """Finds saved prompt templates: long prompts whose text repeats across three or more prompts."""

    def __init__(self, sessions: list) -> None:
        # Fingerprints every long prompt and remembers the uses and sessions of each fingerprint.
        self.uses: Counter = Counter()
        self.sessions: dict = {}
        self.latest: dict = {}
        for session in sessions:
            for turn in session.turns:
                if len(turn.text) < TEMPLATE_MIN_CHARS:
                    continue
                key = self.fingerprint(turn.text)
                self.uses[key] += 1
                self.sessions.setdefault(key, set()).add(id(session))
                stamp = turn.ts.timestamp() if turn.ts else 0.0
                if key not in self.latest or stamp >= self.latest[key][0]:
                    self.latest[key] = (stamp, session, turn)

    @staticmethod
    def fingerprint(text: str) -> str:
        # The lower-cased, whitespace-collapsed first 300 characters of a prompt.
        return " ".join(text.lower().split())[:300]

    def template_key(self, text: str):
        # The fingerprint when text is a saved template (long, used 3+ times), else None.
        if len(text) < TEMPLATE_MIN_CHARS:
            return None
        key = self.fingerprint(text)
        return key if self.uses[key] >= TEMPLATE_MIN_USES else None

    def templates(self) -> list:
        # Saved templates, most used first: {"key", "uses", "sessions", "session", "turn", "title"}.
        found = []
        for key, uses in self.uses.most_common():
            if uses < TEMPLATE_MIN_USES:
                break
            _stamp, session, turn = self.latest[key]
            found.append({"key": key, "uses": uses, "sessions": len(self.sessions[key]), "session": session, "turn": turn, "title": self.title(turn.text)})
        return found

    @staticmethod
    def title(text: str) -> str:
        # The template's first non-empty line without Markdown heading marks, up to 80 characters.
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return TextTools.one_line(line, 80)
        return ""


class Signal:
    """A signal with its hits, ranked examples, and counts."""

    def __init__(self, definition: SignalDefinition, hits: list) -> None:
        # Counts prompts and sessions; a collapsed template hit counts all of its uses.
        self.definition = definition
        self.hits = hits
        self.template_count = sum(1 for h in hits if h.context.get("template_uses"))
        plain_sessions = {id(h.session) for h in hits if not h.context.get("template_uses")}
        self.session_count = len(plain_sessions) + sum(h.context.get("template_sessions", 0) for h in hits)
        self.rank_weight = len({id(h.session) for h in hits})  # A template counts once, so one file cannot top the ranking.
        if definition.scope == "session":
            self.prompt_count = sum(len(h.context.get("turns") or []) or 1 for h in hits)
        else:
            self.prompt_count = sum(h.context.get("template_uses", 1) for h in hits)

    @property
    def id(self) -> str:
        # The definition's id, such as "correction".
        return self.definition.id

    def examples(self, limit: int) -> list:
        # Ranks hits: one per session first, most recent first, clearest asks first for corrections.
        prefer_clear = self.id in ("correction", "restated")

        def key(hit):
            clear = prefer_clear and TextTools.word_count(hit.context.get("prev_text", "")) < CLEAR_ASK_WORDS and hit.context.get("prev_text")
            return (0 if clear else 1, -hit.when())

        ordered = sorted(self.hits, key=key)
        first, rest, seen = [], [], set()
        for hit in ordered:
            (rest if id(hit.session) in seen else first).append(hit)
            seen.add(id(hit.session))
        return (first + rest)[:limit]


class SignalCatalog:
    """The default signal definitions, in the order of the design doc's table."""

    @staticmethod
    def default() -> list:
        # Returns the fourteen definitions with their rubric IDs and measurement text.
        return [
            SignalDefinition("correction", "Corrections", ("RT1", "RC1", "RX3"), 'A later prompt that opens by correcting the agent ("no,", "that\'s not", "I meant", "why did you", "revert", "stop").'),
            SignalDefinition("interrupted", "Interrupted responses", ("RT3", "RA2", "RA4"), "The user interrupted the agent while it was answering this prompt."),
            SignalDefinition("go_ahead", "Permission round-trips", ("RA1", "RT5"), 'The agent stopped to ask, and the reply was a short confirmation of 8 words or fewer ("yes", "go ahead", "do it").'),
            SignalDefinition("vague_opener", "Underspecified openers", ("RC1", "RT1", "RT3"), "A first prompt under 12 words with no file, path, code, or link, followed later by a correction, interruption, or go-ahead, or one that points at something the agent cannot see (\"fix this\")."),
            SignalDefinition("drip_feed", "Requirements fed in one at a time", ("RC3",), 'A session with 3 or more later prompts that add a requirement ("also", "one more thing", "oh and", "btw").', "session"),
            SignalDefinition("restated", "Restated instructions", ("RX2", "RC2"), 'A prompt that repeats something already said ("I already told you", "as I said", "again,").'),
            SignalDefinition("frustration", "Frustration", ("RC4", "RX3"), 'An exasperated prompt ("wtf", "are you serious", "come on", "useless").'),
            SignalDefinition("long_session", "Very long sessions", ("RC4", "RX2"), "A session with 30 or more prompts.", "session"),
            SignalDefinition("huge_paste", "Huge pastes", ("RC2", "RS2"), "A prompt over 8,000 characters."),
            SignalDefinition("emphasis", "Shouting and emphasis", ("RP3",), "Three or more all-caps words (not acronyms), \"!!!\", or capitalized MUST / NEVER / ALWAYS / CRITICAL / IMPORTANT / DO NOT, outside code."),
            SignalDefinition("emotional_stakes", "Tips, threats, and pleading", ("RP4",), 'Offers, threats, or pleading ("I\'ll tip you", "you\'ll be fired", "my job depends on this", "please please").'),
            SignalDefinition("persona", "Expert personas", ("RR1",), '"You are / act as" plus a superlative title ("world-class", "senior", "10x", "expert").'),
            SignalDefinition("step_by_step", "Step-by-step incantations", ("RQ1", "RQ2"), '"Think step by step", "let\'s think", or "take a deep breath".'),
            SignalDefinition("negative_only", "Only negative directives", ("RP1",), 'Three or more "don\'t / never / stop" directives with no "instead" or "rather", outside code.'),
        ]


class TurnMatchers:
    """Per-prompt checks, each returning a context dict on a hit or None."""

    @staticmethod
    def correction(session, index: int):
        # A non-first prompt whose leading characters correct the agent.
        turn = session.turns[index]
        if index == 0 or not Patterns.CORRECTION.search(turn.text.strip()[:LEAD_CHARS]):
            return None
        return {"prev_text": session.turns[index - 1].text}

    @staticmethod
    def interrupted(session, index: int):
        # The response to this prompt was interrupted; the next prompt usually says why.
        if not session.turns[index].interrupted:
            return None
        return {"next_text": session.turns[index + 1].text} if index + 1 < len(session.turns) else {}

    @staticmethod
    def go_ahead(session, index: int):
        # A short confirmation right after the agent asked a question or for permission.
        turn = session.turns[index]
        asked = turn.after_question or bool(Patterns.PERMISSION_ASK.search(turn.before_agent_text[-220:]))
        text = turn.text.strip()
        if index == 0 or not asked or TextTools.word_count(text) > GO_AHEAD_WORDS or not Patterns.CONFIRMATION.match(text):
            return None
        return {}

    @staticmethod
    def restated(session, index: int):
        # A prompt that says it is repeating an earlier instruction.
        if index == 0 or not Patterns.RESTATED.search(session.turns[index].text[:SCAN_CHARS]):
            return None
        return {"prev_text": session.turns[index - 1].text}

    @staticmethod
    def frustration(session, index: int):
        # An exasperated prompt.
        if not Patterns.FRUSTRATION.search(session.turns[index].text[:SCAN_CHARS]):
            return None
        return {"prev_text": session.turns[index - 1].text} if index else {}

    @staticmethod
    def huge_paste(session, index: int):
        # A prompt longer than 8,000 characters.
        size = len(session.turns[index].text)
        return {"chars": size} if size > HUGE_PASTE_CHARS else None

    @staticmethod
    def emphasis(session, index: int):
        # All-caps shouting, "!!!", or capitalized directives outside code.
        text = Patterns.clean(session.turns[index].text)
        caps = [w for w in Patterns.CAPS_WORD.findall(text) if w not in Patterns.ACRONYMS]
        if len(caps) >= 3 or Patterns.BANGS.search(text) or Patterns.CAPS_DIRECTIVE.search(text):
            return {}
        return None

    @staticmethod
    def emotional_stakes(session, index: int):
        # Tips, threats, or pleading.
        return {} if Patterns.STAKES.search(session.turns[index].text[:CLEAN_CHARS]) else None

    @staticmethod
    def persona(session, index: int):
        # An expert persona with a superlative title.
        return {} if Patterns.PERSONA.search(session.turns[index].text[:SCAN_CHARS * 2]) else None

    @staticmethod
    def step_by_step(session, index: int):
        # A step-by-step reasoning incantation.
        return {} if Patterns.STEP_BY_STEP.search(session.turns[index].text[:CLEAN_CHARS]) else None

    @staticmethod
    def negative_only(session, index: int):
        # Three or more negative directives with no positive alternative.
        text = Patterns.clean(session.turns[index].text)
        if TextTools.word_count(text) > NEGATIVE_MAX_WORDS:
            return None
        if len(Patterns.NEGATIVE.findall(text)) >= 3 and not Patterns.NEGATIVE_ESCAPE.search(text):
            return {}
        return None

    @staticmethod
    def is_vague_opener_text(text: str) -> bool:
        # A short first prompt with no file, path, code, or link (slash commands and $skills excluded).
        stripped = text.strip()
        if not stripped or stripped[0] in "/$@":
            return False
        return TextTools.word_count(stripped) < SHORT_OPENER_WORDS and not Patterns.PATH_OR_CODE.search(stripped)


class SignalDetector:
    """Runs every signal over every session and ranks the results."""

    TURN_SIGNALS = ("correction", "interrupted", "go_ahead", "restated", "frustration", "huge_paste", "emphasis", "emotional_stakes", "persona", "step_by_step", "negative_only")
    VAGUE_FOLLOWERS = ("correction", "interrupted", "go_ahead")

    def __init__(self, definitions: list = None) -> None:
        # Uses the default catalog unless a test passes its own definitions.
        self.definitions = definitions if definitions is not None else SignalCatalog.default()
        self._by_id = OrderedDict((d.id, d) for d in self.definitions)

    def detect(self, sessions: list, templates: "TemplateIndex" = None) -> list:
        # Returns non-empty Signals ranked by sessions affected (a template once), then by prompt count.
        templates = templates or TemplateIndex(sessions)
        hits = OrderedDict((d.id, []) for d in self.definitions)
        for session in sessions:
            self._detect_session(session, hits)
        for signal_id in TEMPLATE_SENSITIVE:
            if signal_id in hits:
                hits[signal_id] = self._collapse_templates(hits[signal_id], templates)
        signals = [Signal(self._by_id[sid], found) for sid, found in hits.items() if found]
        order = list(self._by_id)
        return sorted(signals, key=lambda s: (-s.rank_weight, -s.prompt_count, order.index(s.id)))

    def _collapse_templates(self, hits: list, templates: "TemplateIndex") -> list:
        # Keeps one hit (the most recent) per saved template, carrying its uses and sessions.
        kept, groups = [], OrderedDict()
        for hit in hits:
            key = templates.template_key(hit.turn.text) if hit.turn is not None else None
            if key is None:
                kept.append(hit)
            else:
                groups.setdefault(key, []).append(hit)
        for key, group in groups.items():
            latest = max(group, key=lambda h: h.when())
            latest.context = dict(latest.context, template_uses=len(group), template_sessions=len({id(h.session) for h in group}), template_title=TemplateIndex.title(latest.turn.text))
            kept.append(latest)
        return kept

    def turn_signals(self, signals: list) -> dict:
        # Maps (id(session), turn index) to the signal ids that hit that prompt, for markers and the index.
        marks: dict = {}
        for signal in signals:
            for hit in signal.hits:
                turns = [hit.turn] if hit.turn is not None else hit.context.get("turns") or []
                for turn in turns:
                    marks.setdefault((id(hit.session), turn.index), []).append(signal.id)
        return marks

    def overall(self, sessions: list, signals: list = None) -> dict:
        # Computes the overall numbers: volumes, opener shape, session length, and per-100 rates.
        return OverallStats(sessions, signals or []).compute()

    def _detect_session(self, session, hits: dict) -> None:
        # Runs the turn matchers, then the session-scope and opener checks that depend on them.
        found_here = Counter()
        for index in range(len(session.turns)):
            for signal_id in self.TURN_SIGNALS:
                if signal_id not in hits:
                    continue
                context = getattr(TurnMatchers, signal_id)(session, index)
                if context is not None:
                    hits[signal_id].append(SignalHit(session, session.turns[index], context))
                    found_here[signal_id] += 1
        if "vague_opener" in hits and session.turns:
            opener = session.turns[0]
            followed = any(found_here[s] for s in self.VAGUE_FOLLOWERS)
            if TurnMatchers.is_vague_opener_text(opener.text) and (followed or Patterns.NO_REFERENT.match(opener.text.strip())):
                hits["vague_opener"].append(SignalHit(session, opener, {"followed_by": [s for s in self.VAGUE_FOLLOWERS if found_here[s]]}))
        if "drip_feed" in hits:
            drips = [t for t in session.turns[1:] if Patterns.DRIP.match(t.text.strip())]
            if len(drips) >= DRIP_MIN_PROMPTS:
                hits["drip_feed"].append(SignalHit(session, None, {"turns": drips}))
        if "long_session" in hits and session.prompt_count >= LONG_SESSION_PROMPTS:
            hits["long_session"].append(SignalHit(session, None, {"turns": []}))


class OverallStats:
    """The digest's overall numbers over every kept prompt."""

    def __init__(self, sessions: list, signals: list) -> None:
        # Keeps the sessions and the detected signals (for per-100-prompt rates).
        self.sessions = sessions
        self.signals = {s.id: s for s in signals}

    def compute(self) -> dict:
        # Returns a JSON-ready dict of counts, medians, shares, and rates.
        turns = [t for s in self.sessions for t in s.turns]
        openers = [s.turns[0].text for s in self.sessions if s.turns]
        per_session = sorted(s.prompt_count for s in self.sessions)
        prompts = len(turns)
        return {
            "sessions": len(self.sessions),
            "prompts": prompts,
            "by_harness": self._by_harness(),
            "models": self._models(),
            "median_words_per_prompt": self._median([TextTools.word_count(t.text) for t in turns]),
            "median_words_per_opener": self._median([TextTools.word_count(t) for t in openers]),
            "openers_short_share": self._share(openers, lambda t: TextTools.word_count(t) < SHORT_OPENER_WORDS),
            "openers_naming_file_share": self._share(openers, lambda t: bool(Patterns.PATH_OR_CODE.search(t))),
            "openers_giving_reason_share": self._share(openers, lambda t: bool(Patterns.REASON.search(t[:2000]))),
            "openers_stating_check_share": self._share(openers, lambda t: bool(Patterns.CHECK.search(t[:2000]))),
            "prompts_per_session_median": self._median(per_session),
            "prompts_per_session_p90": per_session[min(len(per_session) - 1, int(round(0.9 * (len(per_session) - 1))))] if per_session else 0,
            "per_100_prompts": {sid: self._per_100(sid, prompts) for sid in ("correction", "interrupted", "go_ahead", "restated")},
        }

    def _by_harness(self) -> dict:
        # Sessions and prompts per harness.
        result: dict = OrderedDict()
        for session in self.sessions:
            entry = result.setdefault(session.harness, {"sessions": 0, "prompts": 0})
            entry["sessions"] += 1
            entry["prompts"] += session.prompt_count
        return dict(result)

    def _models(self) -> dict:
        # The ten models seen in the most sessions, with their session counts.
        seen = Counter()
        for session in self.sessions:
            for model in session.models:
                seen[model] += 1
        return dict(seen.most_common(10))

    def _per_100(self, signal_id: str, prompts: int) -> float:
        # Hits of one signal per 100 prompts, to one decimal.
        signal = self.signals.get(signal_id)
        return round(100.0 * signal.prompt_count / prompts, 1) if signal and prompts else 0.0

    @staticmethod
    def _median(values: list):
        # The median of a list of numbers, or 0 when it is empty.
        if not values:
            return 0
        ordered = sorted(values)
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else round((ordered[middle - 1] + ordered[middle]) / 2.0, 1)

    @staticmethod
    def _share(items: list, test) -> float:
        # The share (0-1, two decimals) of items that pass test.
        return round(sum(1 for i in items if test(i)) / float(len(items)), 2) if items else 0.0
