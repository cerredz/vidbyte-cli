"""Group sessions into chores: similar openers in fresh sessions, repeated on a steady cadence.

A chore is a cluster of sessions whose openers say the same thing (stemmed words weighted by how rare
they are), boosted by the same folder and the same files, commands, domains, and MCP tools. Average
linkage keeps clusters tight. Sessions on one day are one run, and a chore needs at least three runs.
"""

from __future__ import annotations

import datetime as dt
import heapq
import math
import re
import statistics
from collections import Counter, defaultdict

from chores.cadence import CadenceAnalyzer
from chores.signatures import DATA_CLIS, DATA_FILE_EXTS, GO_AHEAD_RE, ActionParser, CommandSignature, PathNormalizer, SessionSignature, TemplatePrefixes, TermExtractor

LINK_THRESHOLD = 0.5
MIN_TEXT_SIMILARITY = 0.2
FOLDER_WEIGHT = 0.15
RESOURCE_WEIGHT = 0.35
UBIQUITOUS_SHARE = 0.4
MAX_ITEMS_PER_KIND = 8
FOLLOW_UP_TURNS = 6
FOLLOW_UP_SIMILARITY = 0.4
AUTOMATED_OPENER_CHARS = 150
AUTOMATED_TIME_SPREAD_MINUTES = 3
AUTOMATED_SHARE = 0.8


class Occurrence:
    """One run of a chore: the sessions that did it, on the day it started."""

    def __init__(self, signatures: list) -> None:
        # Holds the run's sessions, sorted by start time.
        self.signatures = sorted(signatures, key=lambda s: (s.start, s.session.session_id))
        self.start = self.signatures[0].start
        self.day = self.start.astimezone().date()

    def merge(self, other: "Occurrence") -> None:
        # Adds another run's sessions to this run, keeping this run's start day.
        self.signatures = sorted(self.signatures + other.signatures, key=lambda s: (s.start, s.session.session_id))

    @property
    def minutes(self) -> float:
        # Session time spent on this run, summed over its sessions.
        return sum(s.minutes for s in self.signatures)


class Chore:
    """A detected recurring task: its runs, cadence, inputs, outputs, and evidence."""

    def __init__(self, signatures: list, runs: list, cadence) -> None:
        # Holds the cluster; the detector fills the aggregates.
        self.signatures = signatures
        self.runs = runs
        self.cadence = cadence
        self.label_signature = signatures[0]
        self.key_terms: list = []
        self.inputs: dict = {}
        self.outputs: dict = {}
        self.credentials: list = []
        self.follow_ups: list = []
        self.automation_evidence: list = []
        self.cwd = ""
        self.harnesses: Counter = Counter()
        self.score = 0.0

    @property
    def likely_automated(self) -> bool:
        # True when the runs look like an existing scheduled job rather than work typed by hand.
        return bool(self.automation_evidence)

    @property
    def minutes_per_run(self) -> float:
        # The median session time of a run.
        return float(statistics.median([r.minutes for r in self.runs])) if self.runs else 0.0

    @property
    def prompts_per_run(self) -> float:
        # The median number of prompts in a run.
        return float(statistics.median([sum(s.session.prompt_count for s in r.signatures) for r in self.runs])) if self.runs else 0.0


class DetectionResult:
    """What detection found: chores, irregular repeats, likely-automated repeats, and the window it saw."""

    def __init__(self) -> None:
        # Starts empty.
        self.chores: list = []
        self.irregular: list = []
        self.automated: list = []
        self.templates: list = []
        self.first_day = None
        self.last_day = None
        self.stats: Counter = Counter()

    @property
    def span_days(self) -> int:
        # Days between the oldest and newest session read.
        return (self.last_day - self.first_day).days if self.first_day and self.last_day else 0


class SimilarityIndex:
    """Weighted Jaccard similarity between session signatures, over terms and resources."""

    def __init__(self, signatures: list) -> None:
        # Computes inverse document frequencies and drops resources that nearly every session has.
        self.signatures = signatures
        count = len(signatures)
        self.term_idf = self._idf([s.terms for s in signatures], count)
        resource_df = Counter(r for s in signatures for r in s.resources)
        limit = max(3, int(count * UBIQUITOUS_SHARE)) if count >= 10 else count + 1
        self.ubiquitous = {r for r, df in resource_df.items() if df > limit}
        self.resources = [s.resources - self.ubiquitous for s in signatures]
        self.resource_idf = self._idf(self.resources, count)

    def edges(self) -> dict:
        # Scores every pair that shares a term and passes the text threshold; returns {(i, j): score}.
        index = defaultdict(list)
        for i, signature in enumerate(self.signatures):
            for term in signature.terms:
                index[term].append(i)
        pairs = set()
        for members in index.values():
            for a in range(len(members)):
                for b in range(a + 1, len(members)):
                    pairs.add((members[a], members[b]))
        scored = {}
        for i, j in sorted(pairs):
            score = self.score(i, j)
            if score is not None:
                scored[(i, j)] = score
        return scored

    def score(self, i: int, j: int):
        # The combined similarity of two sessions, or None when their openers are too different.
        text = self._weighted(self.signatures[i].terms, self.signatures[j].terms, self.term_idf)
        if text < MIN_TEXT_SIMILARITY:
            return None
        folder = 1.0 if self.signatures[i].same_folder(self.signatures[j]) else 0.0
        resources = self._weighted(self.resources[i], self.resources[j], self.resource_idf)
        return text + FOLDER_WEIGHT * folder + RESOURCE_WEIGHT * resources

    @staticmethod
    def _idf(sets: list, count: int) -> dict:
        # Smoothed inverse document frequency, so rare words weigh more than common ones.
        df = Counter(item for s in sets for item in s)
        return {item: math.log((count + 1.0) / (n + 1.0)) + 1.0 for item, n in df.items()}

    @staticmethod
    def _weighted(a: frozenset, b: frozenset, idf: dict) -> float:
        # Jaccard index where each shared or distinct item counts by its weight.
        if not a or not b:
            return 0.0
        union = sum(idf.get(x, 1.0) for x in a | b)
        return sum(idf.get(x, 1.0) for x in a & b) / union if union else 0.0


class AverageLinkage:
    """Agglomerative clustering that merges the pair of clusters with the highest mean pairwise score."""

    def cluster(self, count: int, edges: dict, threshold: float) -> list:
        # Returns clusters (lists of indexes); a missing edge counts as 0, so chains do not merge.
        members = {i: [i] for i in range(count)}
        sums = {}
        neighbors = defaultdict(set)
        heap = []
        for (i, j), score in edges.items():
            sums[(i, j)] = score
            neighbors[i].add(j)
            neighbors[j].add(i)
            heap.append((-score, i, j))
        heapq.heapify(heap)
        next_id = count
        while heap:
            negative, a, b = heapq.heappop(heap)
            if a not in members or b not in members:
                continue
            mean = sums.get(self._key(a, b), 0.0) / (len(members[a]) * len(members[b]))
            if abs(mean + negative) > 1e-9:
                continue
            if mean < threshold:
                break
            merged = next_id
            next_id += 1
            members[merged] = members.pop(a) + members.pop(b)
            for other in (neighbors.pop(a, set()) | neighbors.pop(b, set())) - {a, b}:
                total = sums.pop(self._key(a, other), 0.0) + sums.pop(self._key(b, other), 0.0)
                neighbors[other].discard(a)
                neighbors[other].discard(b)
                sums[self._key(merged, other)] = total
                neighbors[merged].add(other)
                neighbors[other].add(merged)
                heapq.heappush(heap, (-total / (len(members[merged]) * len(members[other])), min(merged, other), max(merged, other)))
            sums.pop(self._key(a, b), None)
        return sorted((sorted(m) for m in members.values()), key=lambda m: m[0])

    @staticmethod
    def _key(a: int, b: int) -> tuple:
        # An order-independent key for a cluster pair.
        return (a, b) if a < b else (b, a)


class RunGrouper:
    """Turns a cluster's sessions into runs: one per day, and nearby days merged for slow cadences."""

    def group(self, signatures: list) -> list:
        # Returns runs sorted by day; with a median gap of 5+ days, runs closer than a quarter of it merge.
        by_day = defaultdict(list)
        for signature in signatures:
            by_day[signature.start.astimezone().date()].append(signature)
        runs = [Occurrence(by_day[day]) for day in sorted(by_day)]
        if len(runs) < 3:
            return runs
        gaps = [(b.day - a.day).days for a, b in zip(runs, runs[1:])]
        middle = statistics.median(gaps)
        if middle < 5:
            return runs
        limit = int(middle // 4)
        merged = [runs[0]]
        for run in runs[1:]:
            if (run.day - merged[-1].day).days <= limit:
                merged[-1].merge(run)
            else:
                merged.append(run)
        return merged


class InputAggregator:
    """Counts, per run, the files, commands, data sources, credentials, and outside actions a chore used."""

    def __init__(self, extractor: TermExtractor) -> None:
        # Uses the term extractor to group recurring follow-up prompts.
        self.extractor = extractor

    def fill(self, chore: Chore) -> None:
        # Fills the chore's inputs, outputs, credentials, follow-ups, folder, and harness counts.
        run_sets = [self._run_facts(run) for run in chore.runs]
        threshold = 2 if len(chore.runs) >= 2 else 1
        chore.inputs = {
            "files_read": self._top(run_sets, "reads", threshold),
            "folders": self._top(run_sets, "folders", threshold),
            "commands": self._top(run_sets, "commands", threshold),
            "data_sources": self._top(run_sets, "sources", threshold),
        }
        chore.outputs = {
            "files_written": self._top(run_sets, "writes", threshold),
            "external_actions": self._external(chore),
        }
        chore.credentials = self._top(run_sets, "credentials", 1)
        chore.follow_ups = self._follow_ups(chore)
        chore.cwd = Counter(s.session.cwd for s in chore.signatures if s.session.cwd).most_common(1)[0][0] if any(s.session.cwd for s in chore.signatures) else ""
        chore.harnesses = Counter(s.session.harness for s in chore.signatures)

    @staticmethod
    def _run_facts(run: Occurrence) -> dict:
        # The union of one run's facts across its sessions.
        facts = defaultdict(set)
        for signature in run.signatures:
            facts["reads"].update(signature.reads)
            facts["writes"].update(signature.writes)
            facts["commands"].update(signature.commands)
            facts["credentials"].update(signature.credentials)
            facts["folders"].update(f for f in (PathNormalizer.parent(p) for p in signature.reads) if f)
            facts["sources"].update("web: " + d for d in signature.domains)
            facts["sources"].update("MCP: " + m.split(":", 1)[0] for m in signature.mcp)
            facts["sources"].update("%s files" % ext for ext in (PathNormalizer.extension(p) for p in signature.reads) if ext in DATA_FILE_EXTS)
            facts["sources"].update("%s CLI" % c.split(" ", 1)[0] for c in signature.commands if c.split(" ", 1)[0] in DATA_CLIS)
        return facts

    @staticmethod
    def _top(run_sets: list, key: str, threshold: int) -> list:
        # [name, runs] pairs seen in at least `threshold` runs, most frequent first, at most 8.
        counts = Counter(item for facts in run_sets for item in facts.get(key, ()))
        kept = [(name, n) for name, n in counts.items() if n >= threshold]
        kept.sort(key=lambda pair: (-pair[1], pair[0]))
        return [[name, n] for name, n in kept[:MAX_ITEMS_PER_KIND]]

    @staticmethod
    def _external(chore: Chore) -> list:
        # Every outside action seen in any run, with how many runs showed it and where.
        found = {}
        for run_number, run in enumerate(chore.runs):
            for signature in run.signatures:
                for label, turn_index in signature.external:
                    entry = found.setdefault(label, {"action": label, "runs": set(), "evidence": []})
                    entry["runs"].add(run_number)
                    if len(entry["evidence"]) < 3:
                        entry["evidence"].append((signature, turn_index))
        ordered = sorted(found.values(), key=lambda e: (-len(e["runs"]), e["action"]))
        return [{"action": e["action"], "runs": len(e["runs"]), "evidence": e["evidence"]} for e in ordered[:MAX_ITEMS_PER_KIND]]

    def _follow_ups(self, chore: Chore) -> list:
        # Non-opener prompts from the start of each run that recur in at least two runs.
        groups = []
        for run_number, run in enumerate(chore.runs):
            for signature in run.signatures:
                for turn in signature.session.turns[:FOLLOW_UP_TURNS]:
                    if turn.index == signature.opener_index or GO_AHEAD_RE.match(turn.text) or SessionSignature.skill_of(turn.text):
                        continue
                    terms = self.extractor.terms(turn.text[:600])
                    if len(terms) < 3:
                        continue
                    for group in groups:
                        if SimilarityIndex._weighted(terms, group["terms"], {}) >= FOLLOW_UP_SIMILARITY:
                            group["items"].append((run_number, signature, turn))
                            break
                    else:
                        groups.append({"terms": terms, "items": [(run_number, signature, turn)]})
        recurring = [g for g in groups if len({item[0] for item in g["items"]}) >= 2]
        recurring.sort(key=lambda g: -len({item[0] for item in g["items"]}))
        return [[(signature, turn) for _, signature, turn in g["items"]][:3] for g in recurring[:4]]


class ChoreDetector:
    """Finds chores in a list of sessions and measures each one's cadence and inputs."""

    def __init__(self, min_runs: int = 3, today=None) -> None:
        # min_runs is the fewest runs a chore needs (3 to 10); today defaults to the local date.
        self.min_runs = min_runs
        self.today = today or dt.date.today()
        self.extractor = TermExtractor()
        paths = PathNormalizer()
        self.parser = ActionParser(paths, CommandSignature(paths))
        self.analyzer = CadenceAnalyzer()
        self.aggregator = InputAggregator(self.extractor)

    def detect(self, sessions: list) -> DetectionResult:
        # Returns chores ranked strongest first, plus irregular and likely-automated repeats.
        result = DetectionResult()
        signatures = self._signatures(sessions, result)
        if signatures:
            days = [s.start.astimezone().date() for s in signatures]
            result.first_day, result.last_day = min(days), max(days)
        index = SimilarityIndex(signatures)
        edges = index.edges()
        result.stats["linked_pairs"] = len(edges)
        for members in AverageLinkage().cluster(len(signatures), edges, LINK_THRESHOLD):
            if len(members) < self.min_runs:
                continue
            self._consider([signatures[i] for i in members], edges, members, index, result)
        result.chores.sort(key=lambda c: (-c.score, -c.runs[-1].day.toordinal()))
        result.irregular.sort(key=lambda c: (-len(c.runs), -c.runs[-1].day.toordinal()))
        return result

    def _signatures(self, sessions: list, result: DetectionResult) -> list:
        # Signatures of sessions that start a task fresh; continuations and sessions without times are counted and left out.
        templates = TemplatePrefixes([turn.text for session in sessions for turn in session.turns[: SessionSignature.OPENER_TURNS]])
        result.templates = [{"chars": len(prefix), "sessions": templates.sessions[prefix]} for prefix in templates.prefixes]
        kept = []
        for session in sessions:
            if not session.turns:
                result.stats["no_prompts"] += 1
                continue
            signature = SessionSignature(session, self.extractor, self.parser, templates)
            if signature.start is None:
                result.stats["no_time"] += 1
            elif signature.continuation:
                result.stats["continuation"] += 1
            elif not signature.terms:
                result.stats["no_terms"] += 1
            else:
                kept.append(signature)
        kept.sort(key=lambda s: (s.start, s.session.session_id))
        result.stats["candidates"] = len(kept)
        return kept

    def _consider(self, cluster: list, edges: dict, members: list, index: SimilarityIndex, result: DetectionResult) -> None:
        # Groups a cluster's runs, measures its cadence, and files it as a chore, irregular, or automated.
        runs = RunGrouper().group(cluster)
        evidence = self._automation_evidence(cluster, runs)
        if len(runs) < self.min_runs and not evidence:
            return
        cadence = self.analyzer.analyze([r.day for r in runs], [r.start for r in runs], self.today, self.min_runs)
        chore = Chore(cluster, runs, cadence)
        chore.label_signature = self._central(cluster, members, edges)
        chore.key_terms = self._key_terms(cluster, index)
        self.aggregator.fill(chore)
        chore.automation_evidence = evidence
        chore.score = len(runs) * cadence.regularity * math.log(2.0 + chore.minutes_per_run)
        if chore.likely_automated:
            result.automated.append(chore)
        elif cadence.regular:
            result.chores.append(chore)
        else:
            result.irregular.append(chore)

    @staticmethod
    def _central(cluster: list, members: list, edges: dict):
        # The session whose opener is most similar to the rest of the cluster; the latest one on a tie.
        best, best_score = cluster[0], -1.0
        for position, i in enumerate(members):
            total = sum(edges.get((min(i, j), max(i, j)), 0.0) for j in members if j != i)
            tie = abs(total - best_score) <= 1e-9 and None not in (cluster[position].start, best.start) and cluster[position].start > best.start
            if total > best_score + 1e-9 or tie:
                best, best_score = cluster[position], total
        return best

    def _key_terms(self, cluster: list, index: SimilarityIndex) -> list:
        # The cluster's six most telling words, in their most common spelling.
        counts = Counter(t for s in cluster for t in s.terms if not t.startswith("/"))
        ranked = sorted(counts, key=lambda t: (-counts[t] * index.term_idf.get(t, 1.0), t))[:6]
        spellings = defaultdict(Counter)
        for signature in cluster:
            for word in re.findall(r"[a-z][a-z0-9+#-]*", signature.task_text[:600].lower()):
                spellings[self.extractor.stem(word)][word] += 1
        return [spellings[t].most_common(1)[0][0] if spellings[t] else t for t in ranked]

    @staticmethod
    def _automation_evidence(cluster: list, runs: list) -> list:
        # Signs that a scheduled job, not a person, starts these sessions: one long first prompt repeated word for word, with a fixed time or spacing.
        openers = [" ".join(s.opener.text.split()) for s in cluster]
        common, count = Counter(openers).most_common(1)[0]
        if count < max(3, int(math.ceil(AUTOMATED_SHARE * len(openers)))):
            return []
        evidence = []
        skill_run = any(s.skill_name for s in cluster if " ".join(s.opener.text.split()) == common)
        if len(common) >= AUTOMATED_OPENER_CHARS and not skill_run:
            evidence.append("the same %d-character first prompt, word for word, in %d of %d sessions" % (len(common), count, len(openers)))
        starts = sorted(s.start.astimezone() for s in cluster if " ".join(s.opener.text.split()) == common)
        spacing = ChoreDetector._fixed_spacing(starts)
        if spacing:
            evidence.append("the sessions start every %d minutes" % spacing)
        minutes = [r.start.astimezone().hour * 60 + r.start.astimezone().minute for r in runs]
        if len(runs) >= 3 and max(minutes) - min(minutes) <= AUTOMATED_TIME_SPREAD_MINUTES:
            evidence.append("every run starts within %d minutes of %02d:%02d" % (AUTOMATED_TIME_SPREAD_MINUTES, minutes[0] // 60, minutes[0] % 60))
        return evidence

    @staticmethod
    def _fixed_spacing(starts: list) -> int:
        # The common gap in minutes between same-day starts when at least 3 gaps agree within 3 minutes, else 0.
        gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(starts, starts[1:]) if 0 < (b - a).total_seconds() < 86400]
        if len(gaps) < 3:
            return 0
        middle = statistics.median(gaps)
        close = sum(1 for g in gaps if abs(g - middle) <= AUTOMATED_TIME_SPREAD_MINUTES)
        return int(round(middle)) if close >= max(3, AUTOMATED_SHARE * len(gaps)) and middle < 24 * 60 - AUTOMATED_TIME_SPREAD_MINUTES else 0
