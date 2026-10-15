"""Run one scheduled routine headless with Claude Code or Codex: a date guard, dry runs first, and logs.

automate-recurring-chores copies this file unchanged into every routine folder it writes, next to
routine.json and routine-prompt.md. It needs only the Python standard library (3.8 or newer) and
imports nothing else, so the folder keeps working on its own.

  python run_routine.py                  dry run now (read-only), ignoring the date guard
  python run_routine.py --live           live run now (may edit files in the working folder)
  python run_routine.py --scheduled      what the scheduler calls: the date guard, then routine.json's mode
  python run_routine.py --check-date     says whether today is a run day, and runs nothing
  python run_routine.py --print-command  prints the harness command, and runs nothing

The routine never sends, pays, posts, uploads, or files anything by itself: routine-prompt.md
starts with standing rules that forbid it, and a dry run gives the harness read-only permissions.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

KINDS = ("daily", "weekdays", "weekly", "biweekly", "monthly", "quarterly")
MONTHLY_PATTERNS = ("first_business_day", "last_business_day", "end_of_month", "day_of_month")
HARNESSES = ("claude-code", "codex")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
LIVE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write")
CLAUDE_QUERY = "Carry out the routine described in the instructions piped on standard input, following its standing rules exactly."
DEFAULT_TIMEOUT_MINUTES = 45


class BusinessCalendar:
    """Month arithmetic and business days (Monday to Friday; public holidays are not known)."""

    @staticmethod
    def last_day(year: int, month: int) -> int:
        # The number of days in the month.
        return calendar.monthrange(year, month)[1]

    @staticmethod
    def business_index(day: dt.date) -> int:
        # The 1-based position of day among the month's weekdays, or 0 when day is a weekend day.
        if day.weekday() >= 5:
            return 0
        return sum(1 for n in range(1, day.day + 1) if dt.date(day.year, day.month, n).weekday() < 5)

    @staticmethod
    def last_business_day(year: int, month: int) -> dt.date:
        # The last Monday-to-Friday day of the month.
        day = dt.date(year, month, BusinessCalendar.last_day(year, month))
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        return day

    @staticmethod
    def month_index(day: dt.date) -> int:
        # A running month number, so two dates' difference in months is a subtraction.
        return day.year * 12 + day.month - 1

    @staticmethod
    def monday_of(day: dt.date) -> dt.date:
        # The Monday of day's week.
        return day - dt.timedelta(days=day.weekday())


class RunWindow:
    """When a cadence runs: its period, its run window with catch-up, its scheduler triggers, and its next run."""

    def __init__(self, cadence: dict) -> None:
        # Validates a cadence config {kind, pattern, weekday, day, month_in_quarter, anchor}; raises ValueError.
        self.kind = cadence.get("kind")
        self.pattern = cadence.get("pattern")
        self.weekday = cadence.get("weekday")
        self.day = cadence.get("day")
        self.month_in_quarter = cadence.get("month_in_quarter")
        anchor = cadence.get("anchor")
        self.anchor = dt.date.fromisoformat(anchor) if isinstance(anchor, str) and anchor else None
        self._validate()

    def period_key(self, day: dt.date) -> str:
        # The period a date belongs to; a routine runs at most once per period.
        if self.kind in ("daily", "weekdays"):
            return day.isoformat()
        if self.kind == "weekly":
            year, week, _ = day.isocalendar()
            return "%04d-W%02d" % (year, week)
        if self.kind == "biweekly":
            return "B%d" % ((day - BusinessCalendar.monday_of(self.anchor)).days // 14)
        if self.kind == "monthly":
            return "%04d-%02d" % (day.year, day.month)
        return "%04d-Q%d" % (day.year, (day.month - 1) // 3 + 1)

    def in_window(self, day: dt.date) -> bool:
        # True when day is inside the run window, which includes a short catch-up after the target day.
        if self.kind == "daily":
            return True
        if self.kind == "weekdays":
            return day.weekday() < 5
        if self.kind in ("weekly", "biweekly"):
            if not self.weekday <= day.weekday() <= min(self.weekday + 2, 6):
                return False
            return self.kind == "weekly" or ((day - BusinessCalendar.monday_of(self.anchor)).days // 7) % 2 == 0
        last = BusinessCalendar.last_day(day.year, day.month)
        if self.kind == "monthly":
            return self._monthly_window(day, last)
        target = min(self.day, last)
        return (day.month - 1) % 3 + 1 == self.month_in_quarter and target <= day.day <= target + 6

    def next_run(self, after: dt.date):
        # The first run day after `after` that falls in a new period, or None when none comes within 400 days.
        current = self.period_key(after)
        for offset in range(1, 401):
            day = after + dt.timedelta(days=offset)
            if self.in_window(day) and self.period_key(day) != current:
                return day
        return None

    def triggers(self) -> dict:
        # The days a scheduler must fire on so that every window day is covered; the runner's guard does the rest.
        if self.kind == "daily":
            return {"type": "daily"}
        if self.kind == "weekdays":
            return {"type": "weekly", "weekdays": [0, 1, 2, 3, 4]}
        if self.kind in ("weekly", "biweekly"):
            return {"type": "weekly", "weekdays": list(range(self.weekday, min(self.weekday + 2, 6) + 1))}
        if self.kind == "monthly":
            return {"type": "monthly", "days": self._monthly_trigger_days(), "months": None}
        first = self.month_in_quarter
        return {"type": "monthly", "days": self._span_days(self.day, 6), "months": [first, first + 3, first + 6, first + 9]}

    def describe(self) -> str:
        # A plain-English description of the window, such as "on the first business day of each month".
        if self.kind == "daily":
            return "every day"
        if self.kind == "weekdays":
            return "every weekday (Monday to Friday)"
        name = WEEKDAY_NAMES[self.weekday] if self.weekday is not None else ""
        if self.kind == "weekly":
            return "every week on %s (catch-up through %s)" % (name, WEEKDAY_NAMES[min(self.weekday + 2, 6)])
        if self.kind == "biweekly":
            return "every two weeks on %s, counting from the week of %s" % (name, self.anchor.isoformat())
        if self.kind == "monthly":
            return {
                "first_business_day": "on the first business day of each month (catch-up through the third business day)",
                "last_business_day": "on the last business day of each month (catch-up through the month's end)",
                "end_of_month": "in the last three days of each month",
                "day_of_month": "on day %s of each month (catch-up for three days; the last day in shorter months)" % self.day,
            }[self.pattern]
        months = ", ".join(calendar.month_abbr[m] for m in self.triggers()["months"])
        return "once a quarter, in %s, from day %s (catch-up for six days)" % (months, self.day)

    def _monthly_window(self, day: dt.date, last: int) -> bool:
        # The monthly run window for this cadence's pattern.
        if self.pattern == "first_business_day":
            return 1 <= BusinessCalendar.business_index(day) <= 3
        if self.pattern == "last_business_day":
            return day >= BusinessCalendar.last_business_day(day.year, day.month)
        if self.pattern == "end_of_month":
            return day.day >= last - 2
        target = min(self.day, last)
        return target <= day.day <= target + 3

    def _monthly_trigger_days(self) -> list:
        # The calendar days that can hold this pattern's window in any month.
        if self.pattern == "first_business_day":
            return [1, 2, 3, 4, 5]
        if self.pattern in ("last_business_day", "end_of_month"):
            return [26, 27, 28, 29, 30, 31]
        return self._span_days(self.day, 3)

    @staticmethod
    def _span_days(start: int, catch_up: int) -> list:
        # Days start..start+catch_up; a target past day 28 covers 28..31, because short months clamp it to their last day.
        if start > 28:
            return list(range(28, 32))
        return list(range(start, min(start + catch_up, 31) + 1))

    def _validate(self) -> None:
        # Raises ValueError naming the first field that does not fit the cadence kind.
        if self.kind not in KINDS:
            raise ValueError("cadence kind must be one of %s" % ", ".join(KINDS))
        if self.kind in ("weekly", "biweekly") and not (isinstance(self.weekday, int) and 0 <= self.weekday <= 6):
            raise ValueError("a weekly cadence needs weekday 0 (Monday) to 6 (Sunday)")
        if self.kind == "biweekly" and self.anchor is None:
            raise ValueError("a biweekly cadence needs an anchor date")
        if self.kind == "monthly" and self.pattern not in MONTHLY_PATTERNS:
            raise ValueError("a monthly cadence needs pattern %s" % " or ".join(MONTHLY_PATTERNS))
        needs_day = (self.kind == "monthly" and self.pattern == "day_of_month") or self.kind == "quarterly"
        if needs_day and not (isinstance(self.day, int) and 1 <= self.day <= 31):
            raise ValueError("this cadence needs day 1-31")
        if self.kind == "quarterly" and self.month_in_quarter not in (1, 2, 3):
            raise ValueError("a quarterly cadence needs month_in_quarter 1, 2, or 3")


class RoutineConfig:
    """routine.json, checked once at start."""

    REQUIRED = ("label", "cadence", "harness", "harness_path", "workdir", "mode")

    def __init__(self, folder: Path) -> None:
        # Loads routine.json and routine-prompt.md from the routine folder; raises ValueError with the fix.
        self.folder = Path(folder)
        try:
            with open(str(self.folder / "routine.json"), encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError) as error:
            raise ValueError("routine.json is missing or not valid JSON (%s)" % error)
        missing = [key for key in self.REQUIRED if not self.data.get(key)]
        if missing:
            raise ValueError("routine.json lacks %s" % ", ".join(missing))
        if self.data["harness"] not in HARNESSES:
            raise ValueError("routine.json harness must be one of %s" % ", ".join(HARNESSES))
        if self.data["mode"] not in ("dry-run", "live"):
            raise ValueError('routine.json mode must be "dry-run" or "live"')
        self.window = RunWindow(self.data["cadence"])
        try:
            with open(str(self.folder / "routine-prompt.md"), encoding="utf-8") as handle:
                self.prompt = handle.read()
        except OSError as error:
            raise ValueError("routine-prompt.md is missing (%s)" % error)

    def value(self, key: str, default=None):
        # One routine.json value, or default.
        return self.data.get(key, default)


class HarnessCommand:
    """The headless command line for one harness and mode, with the prompt sent on stdin."""

    def __init__(self, config: RoutineConfig) -> None:
        # Keeps the config the command is built from.
        self.config = config

    def argv(self, mode: str) -> list:
        # Returns the argv for a dry run (read-only) or a live run (edits allowed only with the listed tools).
        exe = self.config.value("harness_path")
        if self.config.value("harness") == "claude-code":
            # Claude Code reads piped stdin as context for the -p query; plan mode changes nothing, and
            # dontAsk denies every tool call that is not on the allow list instead of waiting for a person.
            argv = [exe, "-p", CLAUDE_QUERY, "--output-format", "text"]
            if mode == "live":
                return argv + ["--permission-mode", "dontAsk", "--allowedTools", ",".join(self.allowed_tools())]
            return argv + ["--permission-mode", "plan"]
        # codex exec reads the prompt from stdin with "-"; read-only is its default sandbox, stated anyway.
        sandbox = "workspace-write" if mode == "live" else "read-only"
        return [exe, "exec", "--skip-git-repo-check", "--cd", self.config.value("workdir"), "--sandbox", sandbox, "-"]

    def allowed_tools(self) -> list:
        # Claude Code allow rules for a live run: file tools plus each allowed command with and without arguments.
        tools = list(LIVE_TOOLS)
        for command in self.config.value("allowed_commands", []):
            tools += ["Bash(%s)" % command, "Bash(%s *)" % command]
        return tools

    def prompt(self, mode: str, today: dt.date) -> str:
        # The routine prompt with a first line that states the mode and the date of this run.
        if mode == "live":
            header = "Routine run on %s. Mode: LIVE. You may change files in this folder as the routine says. Never pay, send, post, upload, or file anything outside this machine; prepare a draft and stop instead." % today.isoformat()
        else:
            header = "Routine run on %s. Mode: DRY RUN. Change nothing: read the inputs, then report exactly what you would do, which files you would write, and anything that would need the user." % today.isoformat()
        return header + "\n\n" + self.config.prompt


class RunState:
    """state.json: which periods already had a successful run, per mode."""

    def __init__(self, folder: Path) -> None:
        # Loads state.json, or starts empty when it is missing or unreadable.
        self.path = Path(folder) / "state.json"
        try:
            with open(str(self.path), encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {}

    def done(self, mode: str, period: str) -> bool:
        # True when this period already had a successful run in this mode.
        return period in self.data.get(self._key(mode), [])

    def record(self, mode: str, period: str, stamp: str, code: int) -> None:
        # Records a run; a successful one also marks its period done.
        if code == 0:
            periods = self.data.setdefault(self._key(mode), [])
            if period not in periods:
                periods.append(period)
                del periods[:-60]
        self.data["last_run"] = {"at": stamp, "mode": mode, "period": period, "exit_code": code}
        with open(str(self.path), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.data, handle, indent=2)
            handle.write("\n")

    @staticmethod
    def _key(mode: str) -> str:
        # The state key for a mode.
        return "live_periods" if mode == "live" else "dry_run_periods"


class RoutineRunner:
    """Parses the runner's options, applies the date guard, runs the harness, and logs the run."""

    def __init__(self, folder: Path) -> None:
        # The routine folder holds routine.json, routine-prompt.md, logs/, and state.json.
        self.folder = Path(folder)

    def main(self, argv: list) -> int:
        # Runs the command line; returns the exit code (0 also when today is not a run day).
        args = self._parse(argv)
        try:
            config = RoutineConfig(self.folder)
            today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        except ValueError as error:
            print("run_routine: %s" % error, file=sys.stderr)
            return 2
        mode = "live" if args.live else ("dry-run" if not args.scheduled else config.value("mode"))
        command = HarnessCommand(config)
        if args.print_command:
            print(json.dumps({"mode": mode, "argv": command.argv(mode), "cwd": config.value("workdir")}, indent=2))
            return 0
        if args.check_date or args.scheduled:
            due, reason = self._due(config, mode, today)
            if args.check_date or not due:
                print(json.dumps({"today": today.isoformat(), "run_day": due, "reason": reason, "next_run": self._next(config, today)}))
                return 0
        return self._run(config, command, mode, today)

    def _due(self, config: RoutineConfig, mode: str, today: dt.date):
        # Returns (due, reason): inside the window and not yet run successfully this period.
        if not config.window.in_window(today):
            return False, "outside the run window (%s)" % config.window.describe()
        period = config.window.period_key(today)
        if RunState(self.folder).done(mode, period):
            return False, "already ran this period (%s)" % period
        return True, "inside the run window (%s)" % config.window.describe()

    @staticmethod
    def _next(config: RoutineConfig, today: dt.date) -> str:
        # The next run day after today, as an ISO date or "".
        upcoming = config.window.next_run(today)
        return upcoming.isoformat() if upcoming else ""

    def _run(self, config: RoutineConfig, command: HarnessCommand, mode: str, today: dt.date) -> int:
        # Runs the harness in the working folder with a timeout, writes the log, and records the period.
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = self.folder / "logs" / ("%s-%s.log" % (stamp, mode))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = command.argv(mode)
        workdir = Path(config.value("workdir"))
        problem = None if Path(argv[0]).is_file() else (127, "the harness was not found at %s" % argv[0])
        if problem is None and not workdir.is_dir():
            problem = (2, "the working folder does not exist: %s" % workdir)
        if problem is not None:
            self._write_log(log_path, mode, today, argv, "", "run_routine: %s\n" % problem[1], problem[0])
            print("run_routine: %s (log: %s)" % (problem[1], log_path), file=sys.stderr)
            return problem[0]
        timeout = int(config.value("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)) * 60
        try:
            done = subprocess.run(argv, input=command.prompt(mode, today).encode("utf-8"), cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as expired:
            code, out, err = 124, expired.stdout or b"", (expired.stderr or b"") + b"\nrun_routine: stopped after the timeout.\n"
        except OSError as error:
            code, out, err = 127, b"", ("run_routine: could not start the harness: %s\n" % error).encode("utf-8")
        self._write_log(log_path, mode, today, argv, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), code)
        RunState(self.folder).record(mode, config.window.period_key(today), stamp, code)
        print(json.dumps({"mode": mode, "exit_code": code, "log": str(log_path)}))
        return code

    @staticmethod
    def _write_log(path: Path, mode: str, today: dt.date, argv: list, out: str, err: str, code: int) -> None:
        # Writes one run's log: the command (without the prompt), the exit code, stdout, and stderr.
        lines = ["date: %s" % today.isoformat(), "mode: %s" % mode, "command: %s" % " ".join(argv), "exit_code: %s" % code, "", "--- output ---", out, "", "--- errors ---", err]
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))

    @staticmethod
    def _parse(argv: list) -> argparse.Namespace:
        # Parses the runner's options; the run modes are mutually exclusive.
        parser = argparse.ArgumentParser(prog="run_routine.py", description="Run this routine headless. A plain run is a dry run.")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--live", action="store_true", help="Run now in live mode (edits allowed with the listed tools).")
        group.add_argument("--scheduled", action="store_true", help="Apply the date guard, then run in routine.json's mode. Schedulers call this.")
        group.add_argument("--check-date", action="store_true", help="Say whether today is a run day, and run nothing.")
        group.add_argument("--print-command", action="store_true", help="Print the harness command, and run nothing.")
        parser.add_argument("--today", default=None, help="Treat this date (YYYY-MM-DD) as today, to test the date guard.")
        return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(RoutineRunner(Path(os.path.dirname(os.path.abspath(__file__)))).main(sys.argv[1:]))
