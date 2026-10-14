"""Name the cadence a chore's run dates show, with its regularity, spread, usual time, and next run date.

The cadence is measured, never guessed: a chore whose intervals are not steady enough is "irregular"
and gets no schedule. The run window logic (what counts as on schedule, and the next run day) is
RunWindow in run_routine.py, the same code the written runner uses for its date guard.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter

from chores.run_routine import BusinessCalendar, RunWindow, WEEKDAY_NAMES

MIN_REGULARITY = 0.6
MIN_RUNS_BY_KIND = {"daily": 5, "weekdays": 5}
MIN_SPAN_BY_KIND = {"daily": 6, "weekdays": 6}
DETECTABLE_SPAN = (("daily", 6), ("weekdays", 6), ("weekly", 14), ("biweekly", 28), ("monthly", 59), ("quarterly", 175))
RUNS_PER_YEAR = {"daily": 365, "weekdays": 260, "weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4}
QUARTER_POSITION = {1: "first", 2: "second", 3: "third"}


class Cadence:
    """A measured cadence (or "irregular") with the numbers behind it."""

    def __init__(self, kind: str, days: list, reason: str = "") -> None:
        # Starts a cadence of one kind for the run days it was measured from.
        self.kind = kind
        self.days = list(days)
        self.intervals = [(b - a).days for a, b in zip(self.days, self.days[1:])]
        self.median_gap = float(statistics.median(self.intervals)) if self.intervals else 0.0
        self.spread = self._mad(self.intervals)
        self.pattern = None
        self.weekday = None
        self.day = None
        self.month_in_quarter = None
        self.anchor = self.days[0].isoformat() if self.days else None
        self.regularity = 0.0
        self.time = ""
        self.next_expected = None
        self.overdue = False
        self.reason = reason

    @property
    def regular(self) -> bool:
        # True for a cadence the runner can schedule.
        return self.kind != "irregular"

    def config(self) -> dict:
        # The cadence config RunWindow and routine.json use.
        return {"kind": self.kind, "pattern": self.pattern, "weekday": self.weekday, "day": self.day, "month_in_quarter": self.month_in_quarter, "anchor": self.anchor}

    def label(self) -> str:
        # A plain-English name for the cadence, such as "monthly, on the first business day".
        if self.kind == "irregular":
            return "no steady cadence (median %s days between runs)" % self._number(self.median_gap)
        if self.kind == "daily":
            return "daily"
        if self.kind == "weekdays":
            return "every weekday"
        if self.kind == "weekly":
            return "weekly, on %ss" % WEEKDAY_NAMES[self.weekday]
        if self.kind == "biweekly":
            return "every two weeks, on %ss" % WEEKDAY_NAMES[self.weekday]
        if self.kind == "monthly":
            return {
                "first_business_day": "monthly, on the first business day",
                "last_business_day": "monthly, on the last business day",
                "end_of_month": "monthly, in the last three days of the month",
                "day_of_month": "monthly, around day %s" % self.day,
            }[self.pattern]
        return "quarterly, in the %s month of each quarter, around day %s" % (QUARTER_POSITION[self.month_in_quarter], self.day)

    def runs_per_year(self) -> int:
        # How many runs a year this cadence implies, or 0 when irregular.
        return RUNS_PER_YEAR.get(self.kind, 0)

    def to_dict(self) -> dict:
        # The JSON form stored in chores.json.
        data = self.config()
        data.update({
            "label": self.label(), "regularity": round(self.regularity, 2), "median_gap": round(self.median_gap, 1), "spread": round(self.spread, 1),
            "intervals": self.intervals, "time": self.time, "runs": len(self.days),
            "next_expected": self.next_expected.isoformat() if self.next_expected else None, "overdue": self.overdue, "reason": self.reason,
        })
        return data

    @staticmethod
    def _mad(values: list) -> float:
        # Median absolute deviation, 0 for fewer than two values.
        if len(values) < 2:
            return 0.0
        middle = statistics.median(values)
        return float(statistics.median([abs(v - middle) for v in values]))

    @staticmethod
    def _number(value: float) -> str:
        # A number without a trailing ".0".
        return ("%.1f" % value).rstrip("0").rstrip(".")


class CadenceAnalyzer:
    """Classifies sorted run days into a cadence and scores how regular it is."""

    def analyze(self, days: list, starts: list, today: dt.date, min_runs: int = 3) -> Cadence:
        # Returns the best-fitting cadence, or an irregular one when the runs are too few or too uneven.
        days = sorted(days)
        if len(days) < max(min_runs, 3):
            return Cadence("irregular", days, "fewer than %d runs" % max(min_runs, 3))
        cadence = self._classify(days)
        if cadence.regular:
            kind = cadence.kind
            if len(days) < max(min_runs, MIN_RUNS_BY_KIND.get(kind, 0)) or (days[-1] - days[0]).days < MIN_SPAN_BY_KIND.get(kind, 0):
                cadence = Cadence("irregular", days, "a %s chore needs at least %d runs over a week" % (kind, MIN_RUNS_BY_KIND.get(kind, 0)))
            elif cadence.regularity < MIN_REGULARITY:
                cadence = Cadence("irregular", days, "only %d%% of the intervals fit %s" % (round(cadence.regularity * 100), cadence.label()))
        cadence.time = self._usual_time(starts)
        if cadence.regular:
            window = RunWindow(cadence.config())
            cadence.next_expected = window.next_run(days[-1])
            cadence.overdue = self._overdue(window, cadence.next_expected, today)
        return cadence

    @staticmethod
    def _overdue(window: RunWindow, expected, today: dt.date) -> bool:
        # True when the expected run's window has passed without a run; still inside its catch-up days is not overdue.
        if expected is None or expected >= today:
            return False
        return window.period_key(expected) != window.period_key(today) or not window.in_window(today)

    @staticmethod
    def detectable(span_days: int) -> list:
        # The cadence kinds a window of span_days can show with three or more runs.
        return [kind for kind, needed in DETECTABLE_SPAN if span_days >= needed]

    def _classify(self, days: list) -> Cadence:
        # Picks the cadence family from the median interval, then measures its fit.
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        middle = statistics.median(gaps)
        if middle <= 1.5:
            return self._daily(days, gaps)
        if 5 <= middle <= 9:
            return self._every(days, gaps, "weekly", 7, 1)
        if 11 <= middle <= 17:
            return self._every(days, gaps, "biweekly", 14, 2)
        if 24 <= middle <= 38:
            return self._monthly(days)
        if 70 <= middle <= 110:
            return self._quarterly(days)
        return Cadence("irregular", days, "the usual interval (%s days) matches no cadence" % Cadence._number(float(middle)))

    def _daily(self, days: list, gaps: list) -> Cadence:
        # Every weekday when no run fell on a weekend and each gap is the next business day; else daily.
        if all(d.weekday() < 5 for d in days):
            cadence = Cadence("weekdays", days)
            cadence.regularity = self._mean([self._business_step(a, b) for a, b in zip(days, days[1:])])
            return cadence
        cadence = Cadence("daily", days)
        cadence.regularity = self._mean([1.0 if g == 1 else 0.5 if g == 2 else 0.0 for g in gaps])
        return cadence

    def _every(self, days: list, gaps: list, kind: str, nominal: int, tolerance: int) -> Cadence:
        # Weekly or biweekly: the usual weekday, and gaps of one period (or two, a skip that counts half).
        cadence = Cadence(kind, days)
        cadence.weekday = self._mode([d.weekday() for d in days])
        scores = []
        for gap in gaps:
            if abs(gap - nominal) <= tolerance:
                scores.append(1.0)
            elif abs(gap - 2 * nominal) <= tolerance + 1:
                scores.append(0.5)
            else:
                scores.append(0.0)
        same_weekday = sum(1 for d in days if d.weekday() == cadence.weekday) / float(len(days))
        cadence.regularity = self._mean(scores) * (1.0 if same_weekday >= 0.6 else same_weekday)
        return cadence

    def _monthly(self, days: list) -> Cadence:
        # Monthly: month steps of one (two is a skip) and a day pattern the runs share.
        cadence = Cadence("monthly", days)
        steps = [BusinessCalendar.month_index(b) - BusinessCalendar.month_index(a) for a, b in zip(days, days[1:])]
        step_score = self._mean([1.0 if s == 1 else 0.5 if s == 2 else 0.0 for s in steps])
        indexes = [BusinessCalendar.business_index(d) for d in days]
        if all(i >= 1 for i in indexes) and sum(1 for i in indexes if i != 1) <= (1 if len(days) >= 4 else 0) and max(indexes) <= 2:
            cadence.pattern, fit = "first_business_day", 1.0
        elif all(d == BusinessCalendar.last_business_day(d.year, d.month) for d in days):
            cadence.pattern, fit = "last_business_day", 1.0
        elif all(d.day >= BusinessCalendar.last_day(d.year, d.month) - 2 for d in days):
            cadence.pattern, fit = "end_of_month", 1.0
        else:
            cadence.pattern = "day_of_month"
            cadence.day = int(round(statistics.median([d.day for d in days])))
            fit = sum(1 for d in days if abs(d.day - min(cadence.day, BusinessCalendar.last_day(d.year, d.month))) <= 3) / float(len(days))
            cadence.spread = max(cadence.spread, Cadence._mad([d.day for d in days]))
        cadence.regularity = step_score * fit
        return cadence

    def _quarterly(self, days: list) -> Cadence:
        # Quarterly: month steps of two to four (six is a skip), in the same month of each quarter.
        cadence = Cadence("quarterly", days)
        steps = [BusinessCalendar.month_index(b) - BusinessCalendar.month_index(a) for a, b in zip(days, days[1:])]
        step_score = self._mean([1.0 if 2 <= s <= 4 else 0.5 if 5 <= s <= 7 else 0.0 for s in steps])
        positions = [(d.month - 1) % 3 + 1 for d in days]
        cadence.month_in_quarter = self._mode(positions)
        cadence.day = int(round(statistics.median([d.day for d in days])))
        same = sum(1 for p in positions if p == cadence.month_in_quarter) / float(len(days))
        cadence.regularity = step_score * same
        return cadence

    @staticmethod
    def _business_step(a: dt.date, b: dt.date) -> float:
        # 1 when b is the next business day after a, 0.5 when one business day was skipped, else 0.
        between = 0
        day = a + dt.timedelta(days=1)
        while day < b:
            if day.weekday() < 5:
                between += 1
            day += dt.timedelta(days=1)
        return 1.0 if between == 0 else 0.5 if between == 1 else 0.0

    @staticmethod
    def _usual_time(starts: list) -> str:
        # The median local start time rounded to 5 minutes, as "HH:MM", or "09:00" with no times.
        minutes = sorted(s.astimezone().hour * 60 + s.astimezone().minute for s in starts if s is not None)
        if not minutes:
            return "09:00"
        value = int(5 * round(statistics.median(minutes) / 5.0)) % (24 * 60)
        return "%02d:%02d" % (value // 60, value % 60)

    @staticmethod
    def _mode(values: list) -> int:
        # The most common value; ties go to the smallest.
        counts = Counter(values)
        best = max(counts.values())
        return min(v for v, c in counts.items() if c == best)

    @staticmethod
    def _mean(values: list) -> float:
        # The arithmetic mean, 0 for an empty list.
        return sum(values) / float(len(values)) if values else 0.0
