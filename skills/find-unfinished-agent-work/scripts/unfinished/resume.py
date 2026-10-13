"""The command that resumes one session in its own harness, run from the session's folder.

Commands, from each tool's documentation: `claude --resume <id>`, `codex resume <id>`,
`opencode --session <id>`, `gemini --resume <id>`, and `qwen --resume <id>`. These live only in the
local index (sessions.json) and the local checklist; they are never sent to the model.
"""

from __future__ import annotations

from pathlib import Path

from .signals import FolderNames

COMMANDS = {
    "claude-code": "claude --resume %s",
    "codex": "codex resume %s",
    "opencode": "opencode --session %s",
    "gemini-cli": "gemini --resume %s",
    "qwen-code": "qwen --resume %s",
}
PER_PROJECT_NOTE = "This tool lists sessions per project folder, so run the command from the session's folder."


class ResumeCommandBuilder:
    """Builds {cwd, command, env, bash, powershell, note} for one session."""

    def __init__(self, home: Path) -> None:
        # home is the user's home folder, used to tell default store folders from custom ones.
        self.home = Path(home)

    def build(self, session) -> dict:
        # The resume entry for a session: the command, the environment it needs, and both shell spellings.
        template = COMMANDS.get(session.harness)
        command = template % session.session_id if template else ""
        env = self._env(session)
        cwd = session.cwd if FolderNames.is_real(session.cwd) else ""
        note = ""
        if not cwd:
            note = "The session's folder is not known; run the command from the project folder."
        elif session.harness in ("gemini-cli", "qwen-code", "claude-code"):
            note = PER_PROJECT_NOTE
        return {
            "cwd": cwd,
            "command": command,
            "env": env,
            "bash": self._bash(cwd, env, command),
            "powershell": self._powershell(cwd, env, command),
            "note": note,
        }

    def _env(self, session) -> dict:
        # The variable that points the harness at a non-default store folder, if one is needed.
        source = Path(str(session.source).split("#", 1)[0])
        if session.harness == "claude-code":
            config = self._ancestor_above(source, "projects")
            if config is not None and not self._same(config, self.home / ".claude"):
                return {"CLAUDE_CONFIG_DIR": str(config)}
        elif session.harness == "codex":
            home = self._ancestor_above(source, "sessions") or self._ancestor_above(source, "archived_sessions")
            if home is not None and not self._same(home, self.home / ".codex"):
                return {"CODEX_HOME": str(home)}
        elif session.harness == "opencode":
            data_dir = self._ancestor_above(source, "storage") if "storage" in source.parts else source.parent
            if data_dir is not None and data_dir.name == "opencode" and not self._same(data_dir, self.home / ".local" / "share" / "opencode"):
                return {"XDG_DATA_HOME": str(data_dir.parent)}
        return {}

    @staticmethod
    def _ancestor_above(path: Path, name: str):
        # The parent of the nearest ancestor folder called name, or None.
        for parent in path.parents:
            if parent.name == name:
                return parent.parent
        return None

    @staticmethod
    def _same(a: Path, b: Path) -> bool:
        # Compares two folders the way the OS would (case-insensitive on Windows).
        return FolderNames.norm(str(a)) == FolderNames.norm(str(b))

    @staticmethod
    def _bash(cwd: str, env: dict, command: str) -> str:
        # bash/zsh form: cd "<cwd>" && VAR="..." <command>.
        prefix = "".join('%s="%s" ' % (k, v) for k, v in env.items())
        return ('cd "%s" && %s%s' % (cwd, prefix, command)) if cwd else prefix + command

    @staticmethod
    def _powershell(cwd: str, env: dict, command: str) -> str:
        # PowerShell form: Set-Location "<cwd>"; $env:VAR = "..."; <command>.
        prefix = "".join('$env:%s = "%s"; ' % (k, v) for k, v in env.items())
        return ('Set-Location "%s"; %s%s' % (cwd, prefix, command)) if cwd else prefix + command
