"""Classifies correction episodes into mistake kinds, clusters them into scenarios, and ranks the scenarios.

A scenario is the same kind of mistake in the same kind of task: shared words in the corrections,
the same tools or file types in what the agent did, or the same project. Clustering is a local first
pass; the Suggestion Agent may still merge two scenarios that turn out to be one mistake.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

FLOOR = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

STOPWORDS = set(
    """
    a an the and or but if then else of to in on at by for with from into onto over under as is are was were be been being am do
    does did done doing have has had having it its it's this that these those there here i i'm me my mine you you're your yours we
    our us they them their he she his her him please pls plz just also so very really can could would should will shall may might
    must let lets let's what which who whom whose when where why how all any some each every no not dont don't do not ok okay yes
    yeah now again still only even more most much many one up out about after before same like make sure want need get got go going
    thing things something stuff way lot bit sure thanks thank hey hi hello always never instead rather stop remember use using used
    keep actually wait nope nah wrong said told already than time times there's that's isn't aren't won't can't didn't doesn't
    meant mean asked ask undo revert back put change changed why did you're think know see look right correct incorrect other
    code file files work working works fix fixed broke broken again first last new old didnt dont doesnt isnt wont cant thats
    """.split()
)
WORD_RE = re.compile(r"[a-z][a-z0-9+#]*(?:[._-][a-z0-9+#]+)*")
KNOWN_TOOLS = set(
    """
    npm pnpm yarn bun npx pip pip3 uv poetry conda pytest jest vitest mocha playwright cypress git gh docker compose kubectl helm
    terraform make cmake cargo rustc go gradle mvn dotnet node deno python python3 py ruby bundle rails rake php composer ruff black
    eslint prettier tsc biome mypy pyright prisma alembic django flask fastapi next nextjs react vue svelte angular tailwind vite
    webpack supabase firebase vercel render aws gcloud az psql mysql sqlite sqlite3 redis mongo mongosh curl powershell bash venv
    virtualenv nvm pyenv brew apt choco winget mongodb postgres postgresql stripe sentry
    """.split()
)
FILE_TYPES = set(
    """
    py ts tsx js jsx mjs cjs json md mdx yaml yml toml ini cfg sh ps1 bat go rs java kt swift rb php cs cpp c h hpp css scss html
    vue svelte sql prisma graphql proto tf lock env txt csv ipynb xml gradle
    """.split()
)
TOKEN_LIKE_RE = re.compile(r"^\d+$|\d{4,}|^(?:sk|pk|rk|ghp|gho|xox[a-z])[-_]")  # numbers and key-like words never become scenario terms
EMAIL_OR_URL_RE = re.compile(r"[\w.%+-]+@[\w.-]+|[a-z][a-z0-9+.-]*://\S+")
PATH_EXT_RE = re.compile(r"[\w./\\-]+\.([A-Za-z0-9]{1,6})(?=$|[\s:,;)\]'\"])")


class MistakeKinds:
    """The plain-English kinds of mistake, and the rules that assign one to an episode."""

    ORDER = ("destructive_action", "stalled", "false_done", "ask_first", "unrequested_change", "wrong_target", "wrong_tool",
             "environment_fix", "convention", "misread_intent", "rejected_approach", "off_track", "other")
    LABELS = {
        "destructive_action": "Destroyed or overwrote work",
        "stalled": "Ran a command that hung until the user stepped in",
        "false_done": "Said it was done or fixed when it was not",
        "ask_first": "Acted without asking first",
        "unrequested_change": "Changed things nobody asked for",
        "wrong_target": "Worked on the wrong file, branch, or place",
        "wrong_tool": "Used the wrong tool or command",
        "environment_fix": "Got the environment wrong; the user supplied the fix",
        "convention": "Broke a project convention",
        "misread_intent": "Misread what the user asked for",
        "rejected_approach": "Chose an approach or design the user rejected",
        "off_track": "Went off track until the user interrupted",
        "other": "Other corrections",
    }
    TEXT_RULES = {
        "destructive_action": re.compile(r"\b(?:deleted|removed|wiped|overwrote|overwritten|lost (?:my|the|all)|force.?push\w*|reset --hard|dropped (?:the|my)|nuked|erased)\b"),
        "stalled": re.compile(r"\b(?:stal+(?:ed|ing|s)?|hung|hangs?|hanging|stuck|froze|frozen|freez\w*|timed? ?out|time-?outs?|not responding|unresponsive|taking (?:too|so|forever) long|never (?:finish|return|complete)\w*)\b"),
        "false_done": re.compile(r"you said .{0,40}(?:done|fixed|works|working|passing)|\btests? (?:fail|are failing|still)|\bdid ?n'?t (?:run|test|verify|check)|\bdid not (?:run|test|verify|check)|\b(?:is|it'?s) not (?:done|working|fixed|finished)\b|\byou (?:have ?n'?t|still have ?n'?t) (?:finished|fixed|done)"),
        "ask_first": re.compile(r"\b(?:don'?t|do not|never) (?:\w+ )?(?:commit|push|merge|deploy|publish|release|open a pr|create a pr)\w*|\bask (?:me )?(?:before|first)\b|\bwithout asking\b|\bi did ?n'?t tell you to\b|\bwait for me\b"),
        "unrequested_change": re.compile(r"\bdid ?n'?t ask\b|\bdid not ask\b|\b(?:don'?t|do not) (?:change|touch|modify|refactor|rewrite)\b|\bleave .{0,30}(?:alone|as is|as it was)\b|\bonly (?:change|touch|edit|modify)\b|\bwhy did you (?:change|add|remove|delete|rewrite|refactor|rename|create|touch)\b"),
        "wrong_target": re.compile(r"\bwrong (?:file|folder|directory|dir|branch|repo|repository|place|path|function|method|class|component|table|column|endpoint|url|project|worktree|account)\b|\bnot (?:that|this) (?:file|one|function|branch|folder)\b|\bthe other (?:file|one|branch|repo)\b"),
        "wrong_tool": re.compile(r"\buse \S+ (?:not|instead of|rather than) \S+|\binstead of (?:npm|yarn|pip|pnpm|bun|poetry|uv|jest|vitest|pytest)\b|\b(?:use|don'?t use|do not use|not) (?:npm|pnpm|yarn|bun|pip|uv|poetry|conda|jest|vitest|pytest|docker|make|gh|curl)\b"),
        "environment_fix": re.compile(r"\b(?:venv|virtualenv|activate|port|env var|environment variable|\.env|path|install(?:ed)?|credentials?|log ?in|token|permission|sudo|docker|node version|python version|wsl)\b"),
        "convention": re.compile(r"\b(?:comments?|naming|name it|style|format(?:ting)?|lint|type hints?|docstrings?|emojis?|indent\w*|conventions?|like the other|follow (?:the|our)|consistent\w*|folder structure|tests? go in)\b"),
        "misread_intent": re.compile(r"\bi (?:meant|said|asked|wanted)\b|\bnot what i\b|\bmisunderstood\b|\bi was asking\b|\bre-?read\b|\bdid ?n'?t answer\b|\bmy (?:actual|main|real|original) questions?\b"),
        "rejected_approach": re.compile(r"\b(?:approach|design|interface|abstraction|architecture|not it|(?:don'?t|do not) like|cleaner|simpler|over-?engineer\w*|too (?:complex|complicated)|modular|hacky|duplicate (?:logic|code))\b"),
    }
    DESTRUCTIVE_ACTION_RE = re.compile(r"rm -rf|rm -r |remove-item .*-recurse|git reset --hard|git push (?:-f|--force)|git checkout -- |git clean|drop table|truncate table|del /s")

    def classify(self, episode) -> str:
        # Returns the first kind in ORDER whose rule matches the episode's corrections and corrected actions.
        text = episode.correction_text()
        signals = episode.signals
        actions = " | ".join(episode.corrected_actions()).lower()
        if self.TEXT_RULES["destructive_action"].search(text) or (episode.strong and self.DESTRUCTIVE_ACTION_RE.search(actions)):
            return "destructive_action"
        if self.TEXT_RULES["stalled"].search(text):
            return "stalled"
        if signals.get("still_broken") or self.TEXT_RULES["false_done"].search(text):
            return "false_done"
        for kind in ("ask_first", "unrequested_change", "wrong_target", "wrong_tool"):
            if self.TEXT_RULES[kind].search(text) or (kind == "unrequested_change" and signals.get("undo")):
                return kind
        if signals.get("fix_after_failure"):
            return "environment_fix" if self.TEXT_RULES["environment_fix"].search(text) else "wrong_tool"
        for kind in ("convention", "misread_intent", "rejected_approach"):
            if self.TEXT_RULES[kind].search(text):
                return kind
        if signals.get("interrupt_redirect"):
            return "off_track"
        return "other"


class EpisodeFeatures:
    """What an episode is about: content words, tools, file types, and project."""

    __slots__ = ("terms", "tools", "file_types", "project", "harness")

    def __init__(self, terms: frozenset, tools: frozenset, file_types: frozenset, project: str, harness: str) -> None:
        # Holds the features used to compare an episode with a scenario.
        self.terms = terms
        self.tools = tools
        self.file_types = file_types
        self.project = project
        self.harness = harness

    @classmethod
    def of(cls, episode) -> "EpisodeFeatures":
        # Extracts the features from the episode's correction texts (emails and links removed) and corrected actions.
        text = EMAIL_OR_URL_RE.sub(" ", episode.correction_text())
        actions = episode.corrected_actions()
        words = WORD_RE.findall(text)
        terms = frozenset(cls.stem(w) for w in words if 3 <= len(w) <= 24 and w not in STOPWORDS and not TOKEN_LIKE_RE.search(w))
        tools = {w for w in words if w in KNOWN_TOOLS}
        for action in actions:
            if ":" in action[:20]:
                tools.update(w for w in WORD_RE.findall(action.split(":", 1)[1].lower()[:120]) if w in KNOWN_TOOLS)
        file_types = set()
        for source in actions + [text]:
            file_types.update(ext.lower() for ext in PATH_EXT_RE.findall(source) if ext.lower() in FILE_TYPES)
        return cls(terms, frozenset(tools), frozenset(file_types), episode.session.project_name(), episode.session.harness)

    @staticmethod
    def stem(word: str) -> str:
        # A light English stemmer, so "installs", "installed", and "installing" compare equal.
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                return word[: -len(suffix)]
        return word


class Scenario:
    """Episodes of one kind of mistake in one kind of task."""

    def __init__(self, kind: str) -> None:
        # Starts an empty scenario of one mistake kind.
        self.kind = kind
        self.episodes: list = []
        self.terms: Counter = Counter()
        self.tools: Counter = Counter()
        self.file_types: Counter = Counter()
        self.projects: Counter = Counter()
        self.harnesses: Counter = Counter()
        self.score = 0.0
        self.tag = ""

    def add(self, episode, features: EpisodeFeatures) -> None:
        # Adds one episode and merges its features into the scenario's counters.
        self.episodes.append(episode)
        self.terms.update(features.terms)
        self.tools.update(features.tools)
        self.file_types.update(features.file_types)
        if features.project:
            self.projects[features.project] += 1
        self.harnesses[features.harness] += 1

    def top_terms(self, count: int = 25) -> set:
        # The scenario's most frequent content words.
        return {term for term, _n in self.terms.most_common(count)}

    @property
    def corrections(self) -> int:
        # Correction prompts across every episode.
        return sum(len(e.corrections) for e in self.episodes)

    @property
    def sessions(self) -> int:
        # Distinct sessions with an episode in this scenario.
        return len({id(e.session) for e in self.episodes})

    @property
    def prompts_lost(self) -> int:
        # Prompts the user spent on corrections in this scenario.
        return sum(e.prompts_lost for e in self.episodes)

    @property
    def tool_calls_redone(self) -> int:
        # Tool calls in the turns the user corrected.
        return sum(e.tool_calls_redone for e in self.episodes)

    @property
    def strong(self) -> int:
        # Strong corrections in this scenario.
        return sum(e.strong for e in self.episodes)

    @property
    def signals(self) -> Counter:
        # Signal counts over every correction in the scenario.
        total: Counter = Counter()
        for episode in self.episodes:
            total.update(episode.signals)
        return total

    @property
    def first_ts(self):
        # The oldest correction time in the scenario.
        stamps = [e.first_ts for e in self.episodes if e.first_ts is not None]
        return min(stamps) if stamps else None

    @property
    def last_ts(self):
        # The newest correction time in the scenario.
        stamps = [e.last_ts for e in self.episodes if e.last_ts is not None]
        return max(stamps) if stamps else None

    def example_episodes(self) -> list:
        # Orders episodes for examples: one per session first, strong ones first, newest first.
        ordered = sorted(self.episodes, key=lambda e: (e.strong > 0, e.last_ts or FLOOR), reverse=True)
        seen, first_pass, rest = set(), [], []
        for episode in ordered:
            (rest if id(episode.session) in seen else first_pass).append(episode)
            seen.add(id(episode.session))
        return first_pass + rest

    def label(self) -> str:
        # A short label: the kind in words, then the scenario's main tools or file types and words.
        anchors = [t for t, _n in self.tools.most_common(3)] or ["." + f for f, _n in self.file_types.most_common(2)]
        words = [t for t, _n in self.terms.most_common(6) if t not in self.tools][:3]
        detail = ", ".join(anchors)
        if words:
            detail = (detail + " - " if detail else "") + ", ".join(words)
        return MistakeKinds.LABELS[self.kind] + (": " + detail if detail else "")


class ScenarioBuilder:
    """Clusters episodes of the same kind into scenarios and ranks them by frequency and cost."""

    def __init__(self, threshold: float = 0.45, kinds: MistakeKinds = None) -> None:
        # threshold is the similarity an episode needs to join an existing scenario.
        self.threshold = threshold
        self.kinds = kinds or MistakeKinds()

    def build(self, episodes: list) -> list:
        # Classifies, clusters, scores, and ranks; returns scenarios best first.
        scenarios: list = []
        for episode in sorted(episodes, key=lambda e: e.last_ts or FLOOR, reverse=True):
            episode.kind = self.kinds.classify(episode)
            features = EpisodeFeatures.of(episode)
            best, best_score = None, 0.0
            for scenario in scenarios:
                if scenario.kind != episode.kind:
                    continue
                score = self.similarity(features, scenario)
                if score > best_score:
                    best, best_score = scenario, score
            if best is None or best_score < self.threshold:
                best = Scenario(episode.kind)
                scenarios.append(best)
            best.add(episode, features)
        for scenario in scenarios:
            scenario.score = self.score(scenario)
        scenarios.sort(key=lambda s: (s.score, s.last_ts or FLOOR), reverse=True)
        return scenarios

    @staticmethod
    def similarity(features: EpisodeFeatures, scenario: Scenario) -> float:
        # Word overlap with the scenario's top words, plus bonuses for shared tools, file types, and project.
        top = scenario.top_terms(25)
        union = features.terms | top
        jaccard = len(features.terms & top) / len(union) if union else 0.0
        bonus = 0.30 if features.tools & set(scenario.tools) else 0.0
        bonus += 0.15 if features.file_types & set(scenario.file_types) else 0.0
        bonus += 0.10 if features.project and features.project in scenario.projects else 0.0
        return jaccard + bonus

    @staticmethod
    def score(scenario: Scenario) -> float:
        # Frequency and cost: sessions weigh most, then episodes, corrections, projects, redone work, and strength.
        value = 2.0 * scenario.sessions + len(scenario.episodes) + scenario.corrections + len(scenario.projects)
        value += min(scenario.tool_calls_redone, 50) / 10.0 + 0.5 * scenario.strong
        if scenario.kind == "destructive_action":
            value += 3.0
        return round(value, 2)

