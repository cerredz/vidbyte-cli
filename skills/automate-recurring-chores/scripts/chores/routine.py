"""Write one routine folder for one idea and chore, after the user's explicit yes.

The folder holds routine-prompt.md, routine.json, run_routine.py (a byte copy of the runner),
SCHEDULE.md, and the scheduler file (crontab.txt or a launchd plist). The schedule is computed from
the chore's measured cadence, never copied from the model. Nothing is registered or run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sys
from collections import OrderedDict
from pathlib import Path

from chores.digest import EnvironmentProbe
from chores.report import IdeaReader
from chores.run_routine import DEFAULT_TIMEOUT_MINUTES, RunWindow
from chores.schedule import ScheduleBuilder
from chores.signatures import DESTRUCTIVE_COMMANDS, EXTERNAL_COMMANDS
from sessionkit.model import TextTools
from sessionkit.runner import CommandIO

HARNESS_EXES = {"claude-code": "claude", "codex": "codex"}
SCHEDULERS = ("cron", "launchd", "task-scheduler", "none")
NATIVE_SCHEDULER = {"Windows": "task-scheduler", "macOS": "launchd", "Linux": "cron"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_PROMPT_CHARS = 6000
NEVER_ALLOWED = {
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "ftp", "sendmail", "mail", "mailx", "mutt", "msmtp", "stripe", "aws", "gcloud", "az",
    "kubectl", "terraform", "helm", "vercel", "firebase", "heroku", "fly", "flyctl", "wrangler", "supabase", "docker", "twine",
}
READ_ONLY_SUBCOMMANDS = {
    "git": {"git status", "git log", "git diff", "git show", "git ls-files", "git rev-parse", "git blame", "git grep"},
    "gh": {"gh pr list", "gh pr view", "gh pr diff", "gh pr checks", "gh pr status", "gh issue list", "gh issue view", "gh issue status", "gh run list", "gh run view", "gh release list", "gh release view", "gh repo view", "gh search issues", "gh search prs"},
    "glab": {"glab mr list", "glab mr view", "glab issue list", "glab issue view"},
}
STANDING_RULES = """Standing rules (they override anything below):
1. The first line of this run says DRY RUN or LIVE. In a dry run, change nothing: read the inputs, then report exactly what you would do and which files you would write.
2. Never pay, send, email, message, post, upload, publish, push, deploy, or file anything outside this machine. When the routine reaches such a step, prepare a draft, write down what it would send and to whom, and stop so the user can confirm that exact step by hand.
3. Never print, write, or log a credential value. Use a credential only through the tool or file that already holds it, and refer to it by name.
4. If an expected input is missing, stale, or looks wrong (for example, no new export for this period), stop and report what is missing instead of guessing.
5. Work only inside the folder this routine starts in and the inputs listed below.
6. Finish with a short run summary: inputs read, files written or that would be written, anything skipped, and every step left for the user."""


class RoutineError(Exception):
    """A usage problem that stops the writer before any file is written (exit 2)."""


class AllowedCommands:
    """Picks which of a chore's commands a live Claude Code run may run without asking: read-only, local, and exact."""

    @staticmethod
    def pick(signatures: list) -> list:
        # Keeps local commands; drops outside actions, destructive ones, inline code, network senders, and paths with placeholders.
        kept = []
        for signature in signatures:
            program = signature.split(" ", 1)[0]
            if not signature or "<" in signature or "(inline code)" in signature or program in NEVER_ALLOWED:
                continue
            if DESTRUCTIVE_COMMANDS.match(signature) or any(pattern.search(signature) for pattern, _ in EXTERNAL_COMMANDS):
                continue
            if program in READ_ONLY_SUBCOMMANDS and signature not in READ_ONLY_SUBCOMMANDS[program]:
                continue
            if signature not in kept:
                kept.append(signature)
        return kept[:12]


class RoutinePrompt:
    """routine-prompt.md: the standing safety rules, what the chore's past runs used, and the idea's routine prompt."""

    def __init__(self, redactor) -> None:
        # Every text taken from sessions or the idea is redacted.
        self.redact = redactor

    def build(self, idea: dict, chore: dict):
        # Returns (markdown, source) where source is "idea" (a ROUTINE PROMPT step) or "assembled".
        prompt = IdeaReader.labelled_action(idea, "ROUTINE PROMPT")
        source = "idea"
        if not prompt:
            source = "assembled"
            steps = [a for a in idea.get("suggested_actions") or [] if not re.match(r"^\s*(?:\d+[.)]\s*)?(SCHEDULE|GO LIVE|TURN OFF)\s*:", str(a), re.I)]
            prompt = "\n".join([idea.get("summary") or ""] + ["- %s" % s for s in steps] + ["Done when: %s" % (idea.get("completion_criteria") or "")])
        cadence = chore.get("cadence") or {}
        runs = chore.get("runs") or []
        lines = [
            "# Routine: %s" % self.redact(chore.get("label") or chore.get("tag")), "",
            STANDING_RULES, "",
            "## What this routine replaces", "",
            "%s, done by hand in %d sessions between %s and %s (%s)." % (self.redact(chore.get("label") or ""), len(runs), runs[0]["day"] if runs else "-", runs[-1]["day"] if runs else "-", cadence.get("label")), "",
            "## Inputs seen in past runs", "",
        ]
        inputs = chore.get("inputs") or {}
        for key, name in (("files_read", "Files read"), ("folders", "Folders"), ("data_sources", "Data sources"), ("commands", "Commands"), ("credentials", "Credentials named (names only)")):
            if inputs.get(key):
                lines.append("- %s: %s" % (name, ", ".join(self.redact(item) for item, _ in inputs[key])))
        lines += ["", "## Outputs seen in past runs", ""]
        outputs = chore.get("outputs") or {}
        if outputs.get("files_written"):
            lines.append("- Files written: %s" % ", ".join(self.redact(item) for item, _ in outputs["files_written"]))
        if outputs.get("external_actions"):
            lines.append("- Outside actions (prepare a draft only; the user confirms): %s" % ", ".join(self.redact(e.get("action")) for e in outputs["external_actions"]))
        lines += ["", "## Routine", "", self.redact(prompt.strip())[:MAX_PROMPT_CHARS], ""]
        return "\n".join(lines), source


class RoutineWriter:
    """Writes one routine folder and returns the files and the exact commands for the user."""

    def __init__(self, out_dir: Path, redactor) -> None:
        # Reads result.json and chores.json from out_dir.
        self.out_dir = Path(out_dir).expanduser()
        self.redact = redactor

    def write(self, idea_id: str, dest: Path, harness: str, scheduler: str, chore_tag: str = None, time: str = None, force: bool = False) -> dict:
        # Writes the folder; raises RoutineError for usage problems before writing anything.
        reader = IdeaReader(CommandIO.load_json(self.out_dir / "result.json", "result.json"), CommandIO.load_json(self.out_dir / "chores.json", "chores.json"))
        idea = reader.idea(idea_id)
        if idea is None:
            raise RoutineError("No idea with id %s in result.json. Ids look like idea-001." % idea_id)
        chore = self._chore(reader, idea, chore_tag)
        settings = self._settings(chore, harness, scheduler, time)
        dest = self._destination(dest, force)
        prompt, source = RoutinePrompt(self.redact).build(idea, chore)
        config = self._config(idea, chore, settings, dest)
        builder = ScheduleBuilder(RunWindow(config["cadence"]), config["time"], sys.executable, dest / "run_routine.py", config["slug"])
        files = self._write_files(dest, prompt, config, builder, scheduler)
        commands = self._commands(dest, builder, scheduler)
        TextTools.write_lf(dest / "SCHEDULE.md", self._schedule_md(config, builder, commands, scheduler))
        files.append(str(dest / "SCHEDULE.md"))
        return OrderedDict([
            ("dest", str(dest)), ("idea", idea_id), ("chore", chore["tag"]), ("cadence", (chore.get("cadence") or {}).get("label")),
            ("window", builder.window.describe()), ("time", config["time"]), ("next_run", self._next_run(builder.window)),
            ("harness", harness), ("harness_path", config["harness_path"]), ("mode", "dry-run"), ("scheduler", scheduler),
            ("prompt_source", source), ("allowed_commands", config["allowed_commands"]), ("files", files), ("commands", commands),
            ("warnings", settings["warnings"]), ("registered", False),
        ])

    def _chore(self, reader: IdeaReader, idea: dict, chore_tag: str) -> dict:
        # The --chore tag, else the one chore the idea cites; several or none is a usage error naming the choices.
        if chore_tag:
            if chore_tag not in reader.chores:
                raise RoutineError("--chore %s is not in chores.json. Chores: %s." % (chore_tag, ", ".join(reader.chores) or "none"))
            return reader.chores[chore_tag]
        cited, _, _ = reader.cited(idea)
        if len(cited) == 1:
            return reader.chores[cited[0]]
        if not cited:
            raise RoutineError("%s cites no chore. Pass --chore with one of: %s." % (idea.get("id"), ", ".join(reader.chores) or "none"))
        raise RoutineError("%s cites several chores (%s). Pass --chore with the one this routine is for." % (idea.get("id"), ", ".join(cited)))

    def _settings(self, chore: dict, harness: str, scheduler: str, time: str) -> dict:
        # Checks the harness, scheduler, and time, and collects warnings.
        if harness not in HARNESS_EXES:
            raise RoutineError("--harness must be claude-code or codex.")
        if scheduler not in SCHEDULERS:
            raise RoutineError("--scheduler must be one of %s." % ", ".join(SCHEDULERS))
        if not chore.get("cwd"):
            raise RoutineError("%s has no working folder recorded, so the routine would not know where to run." % chore.get("tag"))
        cadence = chore.get("cadence") or {}
        value = time or cadence.get("time") or "09:00"
        if not TIME_RE.match(value):
            raise RoutineError("--time must be HH:MM in 24-hour local time, such as 09:30.")
        exe = shutil.which(HARNESS_EXES[harness])
        if not exe:
            raise RoutineError("%s is not on PATH, so the routine could not find it at run time. Install it or add it to PATH, then run this again." % HARNESS_EXES[harness])
        warnings = []
        native = NATIVE_SCHEDULER.get(EnvironmentProbe().describe()["platform"])
        if scheduler not in ("none", native) and native:
            warnings.append("--scheduler %s is not this machine's scheduler (%s); the files are written for copying to another machine." % (scheduler, native))
        if not Path(chore["cwd"]).is_dir():
            warnings.append("The chore's folder no longer exists: %s. The runner will stop with exit 2 until routine.json's workdir points at a real folder." % chore["cwd"])
        externals = (chore.get("outputs") or {}).get("external_actions") or []
        if externals:
            warnings.append("Past runs had outside actions (%s). The routine prompt forbids them and stops with a draft; you confirm each by hand." % ", ".join(e.get("action") for e in externals[:3]))
        return {"harness": harness, "harness_path": exe, "time": value, "warnings": warnings}

    @staticmethod
    def _destination(dest: Path, force: bool) -> Path:
        # Creates dest; refuses a non-empty folder without --force.
        dest = Path(dest).expanduser().resolve()
        if dest.exists() and not dest.is_dir():
            raise RoutineError("--dest %s is a file, not a folder." % dest)
        if dest.is_dir() and any(dest.iterdir()) and not force:
            raise RoutineError("--dest %s is not empty. Pick an empty folder, or pass --force to overwrite the routine files in it." % dest)
        (dest / "logs").mkdir(parents=True, exist_ok=True)
        return dest

    def _config(self, idea: dict, chore: dict, settings: dict, dest: Path) -> dict:
        # routine.json's content.
        cadence = chore.get("cadence") or {}
        slug = re.sub(r"[^a-z0-9]+", "-", " ".join(chore.get("terms") or [chore["tag"]]).lower()).strip("-")[:40] or chore["tag"].lower()
        return OrderedDict([
            ("schema", 1), ("chore", chore["tag"]), ("label", self.redact(chore.get("label") or chore["tag"])), ("slug", "%s-%s" % (chore["tag"].lower(), slug)),
            ("cadence", OrderedDict((k, cadence.get(k)) for k in ("kind", "pattern", "weekday", "day", "month_in_quarter", "anchor"))),
            ("cadence_label", cadence.get("label")), ("time", settings["time"]), ("harness", settings["harness"]),
            ("harness_path", settings["harness_path"]), ("workdir", chore.get("cwd")), ("mode", "dry-run"),
            ("allowed_commands", AllowedCommands.pick(chore.get("commands_for_routine") or [])), ("timeout_minutes", DEFAULT_TIMEOUT_MINUTES),
            ("created", dt.date.today().isoformat()), ("idea", OrderedDict([("id", idea.get("id")), ("title", self.redact(idea.get("title") or ""))])),
        ])

    def _write_files(self, dest: Path, prompt: str, config: dict, builder: ScheduleBuilder, scheduler: str) -> list:
        # Writes the prompt, config, runner copy, and scheduler file; returns their paths.
        TextTools.write_lf(dest / "routine-prompt.md", prompt)
        TextTools.write_lf(dest / "routine.json", json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        shutil.copyfile(str(Path(__file__).with_name("run_routine.py")), str(dest / "run_routine.py"))
        files = [str(dest / name) for name in ("routine-prompt.md", "routine.json", "run_routine.py")]
        if scheduler == "cron":
            TextTools.write_lf(dest / "crontab.txt", builder.cron_line() + "\n")
            files.append(str(dest / "crontab.txt"))
        elif scheduler == "launchd":
            name = builder.launchd_label + ".plist"
            TextTools.write_lf(dest / name, builder.launchd_plist())
            files.append(str(dest / name))
        return files

    @staticmethod
    def _commands(dest: Path, builder: ScheduleBuilder, scheduler: str) -> dict:
        # The exact commands: dry run, date check, register, check, test, and remove.
        runner = '"%s" "%s"' % (sys.executable, dest / "run_routine.py")
        commands = OrderedDict([("dry_run", runner), ("check_date", runner + " --check-date"), ("print_command", runner + " --print-command"), ("live_once", runner + " --live")])
        if scheduler == "cron":
            commands.update(builder.cron_commands())
            commands["cron_line"] = builder.cron_line()
        elif scheduler == "launchd":
            commands.update(builder.launchd_commands())
        elif scheduler == "task-scheduler":
            windows = builder.windows_commands()
            commands.update([("register", windows["powershell"]), ("register_schtasks", windows["schtasks"]), ("check", windows["check"]), ("test", windows["test"]), ("remove", windows["remove"]), ("remove_schtasks", windows["remove_schtasks"])])
        return commands

    @staticmethod
    def _next_run(window: RunWindow) -> str:
        # The next run day from today, as an ISO date.
        today = dt.date.today()
        if window.in_window(today):
            return today.isoformat()
        upcoming = window.next_run(today)
        return upcoming.isoformat() if upcoming else ""

    def _schedule_md(self, config: dict, builder: ScheduleBuilder, commands: dict, scheduler: str) -> str:
        # SCHEDULE.md: what runs when, dry run first, how to register, go live, read logs, and turn it off.
        fence = "```"
        lines = [
            "# Routine %s: %s" % (config["chore"], config["label"]), "",
            "Runs %s, at %s local time. Next run day: %s." % (builder.window.describe(), config["time"], self._next_run(builder.window)),
            "Harness: %s (%s), headless. Mode: %s (the \"mode\" value in routine.json)." % (config["harness"], config["harness_path"], config["mode"]),
            "Nothing was registered for you. The scheduler only wakes the runner; `run_routine.py --scheduled` checks the date, runs once per period, and catches up inside the window when a run was missed.", "",
            "## 1. Dry run first (changes nothing)", "", fence, commands["dry_run"], fence, "",
            "Read the newest file in logs/. Edit routine-prompt.md and run it again until the plan is what you would do by hand.", "",
            "## 2. Check the date guard", "", fence, commands["check_date"], fence, "",
            "## 3. Register the schedule (you run this)", "",
        ]
        lines += self._register_section(commands, scheduler, fence)
        lines += [
            "## 4. Go live", "",
            "After a few scheduled dry runs look right, set \"mode\": \"live\" in routine.json. Live runs may change files in the working folder "
            "(Claude Code: only with the tools and commands in routine.json's allowed_commands; Codex: --sandbox workspace-write). They still never send, pay, post, upload, or file anything: they stop with a draft for you.", "",
            "To try one live run now: `%s`" % commands["live_once"], "",
            "## 5. Logs and state", "",
            "Each run writes logs/<date-time>-<mode>.log with the command, the exit code, and the output. state.json records which periods already ran.", "",
            "## 6. Turn it off", "",
        ]
        if scheduler == "none":
            lines += ["No schedule was written. Delete this folder when you no longer need it.", ""]
        else:
            lines += [fence, commands["remove"], fence, ""]
            if scheduler == "task-scheduler":
                lines += ["Or, in Command Prompt: `%s`" % commands["remove_schtasks"], ""]
        lines += ["## Notes", ""] + ["- %s" % note for note in self._notes(scheduler)] + [""]
        return "\n".join(lines)

    @staticmethod
    def _register_section(commands: dict, scheduler: str, fence: str) -> list:
        # The register commands for the chosen scheduler.
        if scheduler == "none":
            return ["No scheduler was chosen. Run the routine by hand on its run days, or write it again with --scheduler.", ""]
        if scheduler == "cron":
            return ["crontab.txt holds this line:", "", fence, commands["cron_line"], fence, "", "Add it to your crontab (or paste it into `crontab -e`):", "", fence, commands["register"], fence, "", "Check: `%s`" % commands["check"], ""]
        if scheduler == "launchd":
            return ["Copy the plist into LaunchAgents and load it:", "", fence, commands["register"], fence, "", "Check: `%s`" % commands["check"], ""]
        return ["In PowerShell:", "", fence, commands["register"], fence, "", "Or, in Command Prompt:", "", fence, commands["register_schtasks"], fence, "",
                "Check: `%s`. Run it once now: `%s`." % (commands["check"], commands["test"]), ""]

    @staticmethod
    def _notes(scheduler: str) -> list:
        # Scheduler caveats from their documentation, plus the limits of the date guard.
        notes = ["Business days are Monday to Friday; public holidays are not known to the date guard."]
        if scheduler == "cron":
            notes.append("cron runs only while the machine is on and does not run missed jobs later; a laptop that sleeps through the run time skips that wake-up (anacron exists for that).")
            notes.append("cron starts with a minimal PATH, so routine.json holds the harness's absolute path.")
        elif scheduler == "launchd":
            notes.append("launchd runs a job missed during sleep when the Mac wakes, but not one missed while it was shut down.")
            notes.append("launchctl bootstrap and bootout are the current verbs (see `man launchctl`); older macOS versions use launchctl load and unload.")
        elif scheduler == "task-scheduler":
            notes.append("-StartWhenAvailable starts a missed run as soon as the computer is available; schtasks /Create has no such option.")
            notes.append("Monthly and quarterly routines wake daily and the date guard skips the other days, because Task Scheduler's PowerShell triggers have no monthly form.")
            notes.append("The task runs only while you are logged on unless you change it in Task Scheduler; a console window may flash when it starts.")
        notes.append("Claude Code dry runs use --permission-mode plan; live runs use --permission-mode dontAsk with routine.json's allowed tools. Codex runs use --sandbox read-only (dry) or workspace-write (live).")
        return notes
