# vidbyte-cli setup

<!--
Canonical copy. Every public skill in cerredz/vidbyte-cli carries this file at
skills/<slug>/references/setup.md, because `npx skills add ... --skill <slug>` installs one skill
folder on its own and a skill cannot link to a sibling. Edit this copy, then regenerate every
public copy from it. Every command, flag, and message below was checked against
the vidbyte-cli command contract on main (contract commit 9703b53) on 2026-10-11.
-->

Read this file only when a skill's preflight check fails. It walks the user from "nothing
installed" to "the Vidbyte agent this skill needs can run". Run each check, fix only what fails,
and stop as soon as the skill's preflight passes again.

Never ask the user to paste an API key into the chat. A key typed into a conversation is stored in
the transcript. Every step below that needs a secret has the user run a command themselves, or
reads the secret from an environment variable they already set.

## 1. The CLI is installed and new enough

```bash
vidbyte-cli --version
```

If the command is not found, ask the user before installing anything: "It looks like vidbyte-cli
isn't installed yet. Do you want me to install it for you?" Install only after they agree.
`vidbyte-cli` needs Python 3.11 or newer.

```bash
pipx install vidbyte-cli                 # a global command in its own environment (preferred)
python -m pip install vidbyte-cli        # or: into an activated virtual environment
```

If the installed version is older than the skill's `min-cli-version`, upgrade it:

```bash
pipx upgrade vidbyte-cli
python -m pip install -U vidbyte-cli
```

## 2. A Vidbyte API key is stored

```bash
vidbyte-cli --json whoami
```

Exit code 4 with the message `Authentication is required.` means no key is stored. The user
creates a key at <https://vidbyte.pro/settings/api> (creating a key needs a paid plan), then
stores it with one of these:

```bash
vidbyte-cli login                                              # interactive: a hidden prompt
printf '%s' "$VIDBYTE_KEY" | vidbyte-cli login --with-token    # scripts and agents: reads stdin
```

In Claude Code, suggest the user type `! vidbyte-cli login` so the hidden prompt runs in their own
terminal. Alternatively the user can export `VIDBYTE_API_KEY` (a key starts with `vb_live_`)
before starting the coding agent; an environment key wins over a stored one.

| What you see | What it means | What to do |
|---|---|---|
| `Login requires explicit token input in noninteractive mode.` (exit 2) | Login ran without a terminal and without `--with-token`. | Pipe the key to `vidbyte-cli login --with-token`, or have the user run `vidbyte-cli login` themselves. |
| `No usable OS credential store is available.` | The machine has no working keyring (common on headless Linux and in containers). | Retry with `--allow-file-fallback` only after the user approves storing the key in a permission-restricted file. |
| `The API key is missing the scope this command requires.` (exit 4) | The key lacks a scope such as `runtime:write`. | The user grants the scope to the key in the Vidbyte dashboard. |

## 3. The agent's own requirements are met

Each Vidbyte agent runs on a different mix of local tools and keys. Check only the row for the
agent this skill drives.

| Agent | Command | Needs, besides the Vidbyte key |
|---|---|---|
| Suggestion | `vidbyte-cli agents suggest run` | Nothing local: no Codex and no OpenAI key. The Vidbyte key needs the `models:invoke` scope (an admin or full-access key also works), and each model call is billed by usage from the Vidbyte balance: by default gpt-6-sol at high reasoning effort, at its per-token rate plus 2%. |
| Persistent Researcher | `vidbyte-cli research start` | Nothing local. Billed by usage from the Vidbyte balance. |
| Persistent Agent | `vidbyte-cli runtime persistence` | Codex on `PATH` and an OpenAI key stored with `vidbyte-cli provider login openai`. |
| Ensemble | `vidbyte-cli runtime same-host-ensemble` | Codex installed and logged in. |
| Task Board | `vidbyte-cli runtime task-board run` | Codex on `PATH` and a stored OpenAI key. |
| Stages | `vidbyte-cli runtime stages run` | Codex on `PATH` and a stored OpenAI key. |
| Bug Finder | `vidbyte-cli agents bug-finder run` | Codex logged in (`codex login`), a linked GitHub account (`vidbyte-cli github link`), and for `--project-type web` a Browserbase key (`vidbyte-cli browserbase login`). |

Store the OpenAI key the same way as the Vidbyte key, without putting it in the chat:

```bash
vidbyte-cli provider login openai                                       # interactive
printf '%s' "$OPENAI_API_KEY" | vidbyte-cli provider login openai --with-token
vidbyte-cli provider whoami openai                                      # verify
```

`vidbyte-cli github link` uses GitHub's device flow and, today, needs `VIDBYTE_GITHUB_CLIENT_ID`
set to the client ID of a GitHub OAuth app or GitHub App with device flow enabled, because no
Vidbyte GitHub app is registered yet. If that variable is not set, tell the user the bug finder is
not ready for them yet rather than inventing a value.

## 4. Diagnose what is still wrong

```bash
vidbyte-cli doctor            # diagnose CLI setup
vidbyte-cli runtime doctor    # detect the supported coding-agent hosts on this machine
```

Read their output, fix the first failing item, and rerun the skill's preflight.

## 5. Optional: the MCP server

Skills drive `vidbyte-cli` directly and do not need the MCP server. Register it only when the user
asks for it or a skill says it routes through it. It needs [`uv`](https://docs.astral.sh/uv/) and
a released CLI that includes `mcp serve`.

```bash
claude mcp add --scope user vidbyte -- uvx --from "vidbyte-cli[mcp]" vidbyte-cli mcp serve
codex mcp add vidbyte -- uvx --from "vidbyte-cli[mcp]" vidbyte-cli mcp serve
```

For Codex, set `startup_timeout_sec = 60` under `[mcp_servers.vidbyte]` in `~/.codex/config.toml`,
because the first `uvx` launch downloads the package. Never put an API key in the registration
command; clients store that command in plain text.
