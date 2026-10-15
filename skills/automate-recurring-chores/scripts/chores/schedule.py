"""Turn a run window's triggers into exact cron, launchd, and Windows Task Scheduler text.

The scheduler only has to wake the runner on every day that can hold a run; the runner's date guard
(`run_routine.py --scheduled`) decides whether today is a run day and runs once per period. Nothing
here registers a job: the routine writer prints these commands for the user to run.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from xml.sax.saxutils import escape

from chores.run_routine import WEEKDAY_NAMES, RunWindow

CRON_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class ScheduleBuilder:
    """Scheduler text for one routine folder: the cron line, the launchd plist, and the Windows commands."""

    def __init__(self, window: RunWindow, time: str, python: str, runner: Path, label: str) -> None:
        # time is "HH:MM" local; python and runner are absolute paths; label is the routine's slug.
        self.window = window
        self.hour, self.minute = (int(part) for part in time.split(":"))
        self.time = "%02d:%02d" % (self.hour, self.minute)
        self.python = str(python)
        self.runner = Path(runner)
        self.folder = self.runner.parent
        self.label = label
        self.triggers = window.triggers()

    @property
    def launchd_label(self) -> str:
        # The launchd job label, also the plist's file name without ".plist".
        return "com.vidbyte.routine.%s" % self.label

    @property
    def task_name(self) -> str:
        # The Windows task name.
        return "Vidbyte routine %s" % self.label

    def cron_line(self) -> str:
        # One crontab line that wakes the runner on every trigger day at the routine's time.
        days, months, weekdays = "*", "*", "*"
        if self.triggers["type"] == "weekly":
            weekdays = self._cron_list([(w + 1) % 7 for w in self.triggers["weekdays"]])
        elif self.triggers["type"] == "monthly":
            days = self._cron_list(self.triggers["days"])
            months = self._cron_list(self.triggers["months"]) if self.triggers.get("months") else "*"
        log = shlex.quote(str(self.folder / "logs" / "scheduler.log"))
        return "%d %d %s %s %s %s %s --scheduled >> %s 2>&1" % (self.minute, self.hour, days, months, weekdays, shlex.quote(self.python), shlex.quote(str(self.runner)), log)

    def cron_commands(self) -> dict:
        # Install, test, and remove commands for cron; the line itself is saved in crontab.txt.
        crontab = shlex.quote(str(self.folder / "crontab.txt"))
        marker = shlex.quote(str(self.runner))
        return {
            "register": "(crontab -l 2>/dev/null; cat %s) | crontab -" % crontab,
            "check": "crontab -l | grep -F %s" % marker,
            "remove": "crontab -l | grep -vF %s | crontab -" % marker,
        }

    def launchd_plist(self) -> str:
        # A LaunchAgent plist whose StartCalendarInterval lists every trigger day at the routine's time.
        intervals = "".join(self._plist_dict(entry) for entry in self._calendar_entries())
        arguments = "".join("\t\t<string>%s</string>\n" % escape(a) for a in (self.python, str(self.runner), "--scheduled"))
        log = escape(str(self.folder / "logs" / "launchd.log"))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "\t<key>Label</key>\n\t<string>%s</string>\n"
            "\t<key>ProgramArguments</key>\n\t<array>\n%s\t</array>\n"
            "\t<key>WorkingDirectory</key>\n\t<string>%s</string>\n"
            "\t<key>StartCalendarInterval</key>\n\t<array>\n%s\t</array>\n"
            "\t<key>StandardOutPath</key>\n\t<string>%s</string>\n"
            "\t<key>StandardErrorPath</key>\n\t<string>%s</string>\n"
            "</dict>\n</plist>\n"
        ) % (escape(self.launchd_label), arguments, escape(str(self.folder)), intervals, log, log)

    def launchd_commands(self) -> dict:
        # Copy-and-load, test, and remove commands for launchd (see `man launchctl` on the Mac).
        name = self.launchd_label + ".plist"
        target = "~/Library/LaunchAgents/%s" % name
        return {
            "register": "cp %s ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) %s" % (shlex.quote(str(self.folder / name)), target),
            "check": "launchctl print gui/$(id -u)/%s" % self.launchd_label,
            "remove": "launchctl bootout gui/$(id -u)/%s && rm %s" % (self.launchd_label, target),
        }

    def windows_commands(self) -> dict:
        # PowerShell registration (catch-up with -StartWhenAvailable), the schtasks equivalent, and removal.
        action = "$action = New-ScheduledTaskAction -Execute %s -Argument %s -WorkingDirectory %s" % (
            self._ps(self.python), self._ps('"%s" --scheduled' % self.runner), self._ps(str(self.folder)))
        if self.triggers["type"] == "weekly":
            days = ",".join(WEEKDAY_NAMES[w] for w in self.triggers["weekdays"])
            trigger = "$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek %s -At %s" % (days, self.time)
            schtasks_when = "/SC WEEKLY /D %s /ST %s" % (",".join(CRON_DAYS[w] for w in self.triggers["weekdays"]), self.time)
        else:
            # Task Scheduler's PowerShell triggers have no monthly form, so monthly and quarterly routines wake daily and the date guard decides.
            trigger = "$trigger = New-ScheduledTaskTrigger -Daily -At %s" % self.time
            schtasks_when = "/SC DAILY /ST %s" % self.time
        register = "Register-ScheduledTask -TaskName %s -Action $action -Trigger $trigger -Settings $settings -Description %s" % (
            self._ps(self.task_name), self._ps("Runs %s headless: %s. Created by automate-recurring-chores." % (self.label, self.window.describe())))
        powershell = "\n".join([action, trigger, "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable", register])
        command = '\\"%s\\" \\"%s\\" --scheduled' % (self.python, self.runner)
        return {
            "powershell": powershell,
            "schtasks": 'schtasks /Create /TN "%s" /TR "%s" %s' % (self.task_name, command, schtasks_when),
            "check": "Get-ScheduledTask -TaskName %s | Get-ScheduledTaskInfo" % self._ps(self.task_name),
            "test": "Start-ScheduledTask -TaskName %s" % self._ps(self.task_name),
            "remove": "Unregister-ScheduledTask -TaskName %s -Confirm:$false" % self._ps(self.task_name),
            "remove_schtasks": 'schtasks /Delete /TN "%s" /F' % self.task_name,
        }

    def _calendar_entries(self) -> list:
        # launchd calendar dicts: Weekday uses 0 for Sunday, so Monday (Python 0) is 1.
        base = {"Hour": self.hour, "Minute": self.minute}
        if self.triggers["type"] == "daily":
            return [base]
        if self.triggers["type"] == "weekly":
            return [dict(base, Weekday=(w + 1) % 7) for w in self.triggers["weekdays"]]
        months = self.triggers.get("months")
        days = [dict(base, Day=d) for d in self.triggers["days"]]
        return [dict(entry, Month=m) for m in months for entry in days] if months else days

    @staticmethod
    def _plist_dict(entry: dict) -> str:
        # One <dict> of integer calendar keys, in launchd's usual key order.
        order = ("Month", "Day", "Weekday", "Hour", "Minute")
        body = "".join("\t\t\t<key>%s</key>\n\t\t\t<integer>%d</integer>\n" % (key, entry[key]) for key in order if key in entry)
        return "\t\t<dict>\n%s\t\t</dict>\n" % body

    @staticmethod
    def _cron_list(values: list) -> str:
        # A cron field: a range for consecutive values ("1-5"), else a comma list.
        values = sorted(set(values))
        if len(values) > 2 and values == list(range(values[0], values[-1] + 1)):
            return "%d-%d" % (values[0], values[-1])
        return ",".join(str(v) for v in values)

    @staticmethod
    def _ps(value: str) -> str:
        # A PowerShell single-quoted string.
        return "'%s'" % str(value).replace("'", "''")
