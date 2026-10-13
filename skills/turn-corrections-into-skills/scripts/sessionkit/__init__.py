"""Read local coding-agent sessions into one harness-agnostic model.

sessionkit is shared, byte for byte, by every public vidbyte-cli skill that reads the user's
Claude Code, Codex, OpenCode, Gemini CLI, and Qwen Code sessions. A skill folder is installed on
its own, so each skill carries its own copy; edit the copy in the skill that owns it
(improve-my-prompting) and re-copy it everywhere, never one copy by hand.

Only the Python standard library is used, and Python 3.8 or newer is enough. sqlite3 is
optional: without it, OpenCode's database is skipped and reported.
"""

SESSIONKIT_VERSION = "2"
