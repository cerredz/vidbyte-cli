# Vidbyte CLI

[![PyPI](https://img.shields.io/pypi/v/vidbyte-cli)](https://pypi.org/project/vidbyte-cli/)
[![Python](https://img.shields.io/pypi/pyversions/vidbyte-cli)](https://pypi.org/project/vidbyte-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

The terminal client for Vidbyte: run research threads, Vidbyte's specialized agents, and local
runtime primitives from your shell. Every command can emit JSON, so coding agents can drive it too.

> This repository is the CLI's public home: install instructions, releases, and the issue
> tracker. The package itself is published to [PyPI](https://pypi.org/project/vidbyte-cli/).

## Install

Requires Python 3.11 or newer.

```bash
pipx install vidbyte-cli                    # a global command in its own environment
python -m pip install vidbyte-cli           # or: into an activated virtual environment
vidbyte-cli --help
```

Upgrade with `pipx upgrade vidbyte-cli` or `python -m pip install -U vidbyte-cli`.

## Set up from Claude Code or Codex

The `vidbyte` plugin teaches your coding agent to install `vidbyte-cli`, log you in, and check the
account, asking before every install. Your API key never enters the chat.

```bash
claude plugin install vidbyte --marketplace cerredz/vidbyte-cli    # Claude Code
codex plugin marketplace add cerredz/vidbyte-cli                   # Codex
codex plugin add vidbyte@vidbyte
```

Then ask your agent: "Set me up on Vidbyte." Details are in [plugins/vidbyte](plugins/vidbyte).

## Quickstart

```bash
vidbyte-cli login                           # store your Vidbyte API key
vidbyte-cli doctor                          # check configuration and credentials
vidbyte-cli research start "your question"  # open a research thread
vidbyte-cli research threads                # list your threads
```

The step-by-step guide lives at <https://vidbyte.pro/docs/cli/quickstart>.

## Commands

| Command | Purpose |
| --- | --- |
| `vidbyte-cli login` / `logout` / `whoami` | Manage the stored Vidbyte API key |
| `vidbyte-cli research start\|add\|resume` | Open a thread, add a run, or continue a run |
| `vidbyte-cli research status\|watch\|threads\|thread` | Read run and thread state |
| `vidbyte-cli agents suggest run --goal "..."` | Generate and critique ranked next-action ideas |
| `vidbyte-cli runtime list` / `doctor` | List local runtime primitives and detect agent hosts |
| `vidbyte-cli runtime persistence <task>` | Run one Codex session with additional improvement turns |
| `vidbyte-cli billing top-up --confirm` | Add Vidbyte API balance (only with your approval) |
| `vidbyte-cli provider login\|logout\|whoami` | Manage bring-your-own-key provider keys |
| `vidbyte-cli config get\|set` | Manage CLI configuration |
| `vidbyte-cli star` | Star this repository on GitHub |

Root options go before the command, for example `vidbyte-cli --json research threads`.
Run `vidbyte-cli <command> --help` for the full reference.

## Support the project

If the CLI is useful to you, star it from your terminal:

```bash
vidbyte-cli star              # stars through the GitHub CLI (gh) when it is logged in
vidbyte-cli star --browser    # or opens this page so you can click Star yourself
```

## Feedback

- **Bugs and feature requests:** [open an issue](https://github.com/cerredz/vidbyte-cli/issues/new/choose).
- **Security problems:** please don't open a public issue; see [SECURITY.md](SECURITY.md).
- **Release notes:** [Releases](https://github.com/cerredz/vidbyte-cli/releases).

## License

[MIT](LICENSE)
