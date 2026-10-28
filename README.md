# Vidbyte CLI and agent skills

[![PyPI](https://img.shields.io/pypi/v/vidbyte-cli)](https://pypi.org/project/vidbyte-cli/)
[![Python](https://img.shields.io/pypi/pyversions/vidbyte-cli)](https://pypi.org/project/vidbyte-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Vidbyte CLI: start hosted Research harness runs, run local Codex coding-agent teams, and get ranked next actions from your terminal.

This repository is the public home of two things:

- **`vidbyte-cli`**, Vidbyte's terminal app for AI-agent work. It starts hosted, persistent
  research runs on Vidbyte's servers (the
  [Vidbyte Research harness](https://vidbyte.pro/docs/harnesses/research), whose agent is the
  Persistent Researcher), runs teams of local Codex coding agents on your machine (Ensemble
  Agent, Persistent Agent, Task Board, Stages Agent), and turns a goal into ranked,
  critic-reviewed next actions (Suggestion Agent). Its commands print JSON with `--json`, so
  Claude Code and Codex can drive it. The package is published to
  [PyPI](https://pypi.org/project/vidbyte-cli/); this repository holds its install
  instructions, releases, and issue tracker.
- **Vidbyte's agent skills**, in [`skills/`](skills): instructions that teach Claude Code,
  Codex, and other coding agents to use `vidbyte-cli` for a job, such as researching a
  competitor or resolving the merge conflicts on your pull requests.

## What you can do

Each job below names the command or skill that does it in vidbyte-cli 0.2.0, the current PyPI
release.

### Persistent deep research from the terminal, alongside Claude Code or Codex

`vidbyte-cli research start` starts a run of the hosted
[Research harness](https://vidbyte.pro/docs/harnesses/research) on Vidbyte's servers, so the
research keeps going after you close the terminal. Each run belongs to a thread you can add to or
resume later, and it is never billed past its cap (`--max-cost-cents`, $5.00 by default). With
`--json`, Claude Code or Codex can start a run and follow it with `research watch`.

```bash
vidbyte-cli --json research start "How do developer tools price usage-based plans?" --max-cost-cents 500
```

### Run several Codex agents in parallel and implement the best approach

`vidbyte-cli runtime same-host-ensemble` runs the
[Ensemble Agent](https://vidbyte.pro/agents/ensemble-agent) on your machine. A planner invents
specialist roles, every role proposes approaches at the same time in its own read-only Codex
fork, a selector narrows them to one, and a single writer implements it in your workspace. It
needs Codex installed and logged in, and costs 2¢ per launch plus your own Codex usage.

```bash
vidbyte-cli runtime same-host-ensemble "Add retries with backoff to the HTTP client" --roles 3
```

### Turn a goal into ranked, critiqued next actions

`vidbyte-cli agents suggest run` runs the
[Suggestion Agent](https://vidbyte.pro/agents/suggestion-agent). It drafts candidate next actions
for one goal, an independent critic rejects the weak and redundant ones, and the survivors come
back ranked, each with its evidence and a handoff another agent can carry out. Options such as
`--constraint` and `--files` add the context only you know.

```bash
vidbyte-cli --json agents suggest run --goal "Get our first 100 paying users" --count 5
```

### One fresh Codex agent per GitHub issue

The [`github-issue-tasks`](https://vidbyte.pro/agents/skills/github-issue-tasks) agent skill gives
each GitHub issue you name its own Codex agent with an empty context window, through the Vidbyte
Task Board. Each issue's changes land on their own local branch, and a draft pull request opens
only for the issues you approve. Install it, then ask your coding agent to "implement issues 12,
31, and 40".

```bash
npx skills add cerredz/vidbyte-cli --skill github-issue-tasks
```

### Keep a Codex session working past its first answer

`vidbyte-cli runtime persistence` runs the
[Persistent Agent](https://vidbyte.pro/agents/persistent-agent): one local Codex session does your
task, then gets a fixed number of further turns that ask it to check its assumptions, find
omissions, and improve the result (6 at `--strength 1`, up to 100 at `--strength 6`). The
strength you choose, not the model saying it is done, decides when it stops. It costs 2¢ per
launch, and Codex runs on your own OpenAI API key.

```bash
vidbyte-cli runtime persistence "Raise test coverage in src/billing to 90 percent" --strength 1
```

## Install

Requires Python 3.11 or newer.

```bash
pipx install vidbyte-cli                    # a global command in its own environment
python -m pip install vidbyte-cli           # or: into an activated virtual environment
vidbyte-cli --help
```

Upgrade with `pipx upgrade vidbyte-cli` or `python -m pip install -U vidbyte-cli`.

## Install the agent skills

```bash
npx skills add cerredz/vidbyte-cli                                  # every skill in skills/
npx skills add cerredz/vidbyte-cli --skill <slug>                   # one skill
npx skills add cerredz/vidbyte-cli --skill research-a-competitor    # for example
```

Each skill names the `vidbyte-cli` version it needs. The guide is at
<https://vidbyte.pro/docs/cli/agent-skills>.

The same skills also install as one plugin, `vidbyte-agent-skills`, from this repository's
marketplace. Its skills then run as `/vidbyte-agent-skills:<slug>` in Claude Code:

```bash
claude plugin install vidbyte-agent-skills --marketplace cerredz/vidbyte-cli    # Claude Code
codex plugin marketplace add cerredz/vidbyte-cli                                # Codex
codex plugin add vidbyte-agent-skills@vidbyte
```

Not the Vidbyte learning skills (the npm package `vidbyte-skills`, cerredz/Vidbyte-Skills).
Those install with `npx -p vidbyte-skills vidbyte-learning-skills --version all`.

| Skill | What it does | vidbyte-cli it needs |
| --- | --- | --- |
| [`app-conversion-tactics`](skills/app-conversion-tactics/SKILL.md) | Get more of your app's users to pay, with tactics checked against App Review | 0.3.2, needs the next vidbyte-cli release (not on PyPI yet) |
| [`automate-recurring-chores`](skills/automate-recurring-chores/SKILL.md) | Turn the chores you redo in coding-agent sessions into scheduled routines | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`cut-my-agent-token-usage`](https://vidbyte.pro/agents/skills/cut-my-agent-token-usage) | Find the coding-agent sessions that use the most tokens, and what to change | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`find-unfinished-agent-work`](skills/find-unfinished-agent-work/SKILL.md) | Find the coding-agent work you started and never finished | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`github-issue-tasks`](https://vidbyte.pro/agents/skills/github-issue-tasks) | Implement the GitHub issues you name, one fresh agent per issue | 0.2.0 (on PyPI) |
| [`improve-my-aeo`](https://vidbyte.pro/agents/skills/improve-my-aeo) | Get your site read, trusted, and cited by answer engines | 0.3.2, needs the next vidbyte-cli release (not on PyPI yet) |
| [`improve-my-prompting`](https://vidbyte.pro/agents/skills/improve-my-prompting) | Critique how you prompt your coding agents, from your own sessions | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`next-actions-from-all-my-notes`](https://vidbyte.pro/agents/skills/next-actions-from-all-my-notes) | Turn what you wrote this week, in every note tool, into next actions | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`next-actions-from-google-docs`](https://vidbyte.pro/agents/skills/next-actions-from-google-docs) | Turn what you wrote in Google Docs into next actions | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`next-actions-from-notion`](https://vidbyte.pro/agents/skills/next-actions-from-notion) | Find your most important next actions in what you wrote in Notion | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`next-actions-from-obsidian`](https://vidbyte.pro/agents/skills/next-actions-from-obsidian) | Turn what you wrote in Obsidian this week into next actions | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`next-actions-from-slack`](https://vidbyte.pro/agents/skills/next-actions-from-slack) | Find your next actions in what you wrote and discussed in Slack | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`pick-up-where-my-sessions-left-off`](https://vidbyte.pro/agents/skills/pick-up-where-my-sessions-left-off) | Pick up where your coding-agent sessions left off | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`predict-the-review`](skills/predict-the-review/SKILL.md) | Predict what reviewers will object to before you open the PR | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`product-ideas-after-merge`](skills/product-ideas-after-merge/SKILL.md) | Get product ideas in Slack after every merged pull request | 0.3.3, needs the next vidbyte-cli release (not on PyPI yet) |
| [`research-a-competitor`](https://vidbyte.pro/agents/skills/research-a-competitor) | Research a competitor, one research run per aspect | 0.2.0 (on PyPI) |
| [`resolve-pr-conflicts`](https://vidbyte.pro/agents/skills/resolve-pr-conflicts) | Resolve the merge conflicts on your open pull requests | 0.2.0 (on PyPI) |
| [`stop-repeating-yourself`](https://vidbyte.pro/agents/skills/stop-repeating-yourself) | Stop having to repeat yourself to your coding agents | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`too-many-meetings`](https://vidbyte.pro/agents/skills/too-many-meetings) | Get your focus time back from too many meetings | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`turn-corrections-into-skills`](https://vidbyte.pro/agents/skills/turn-corrections-into-skills) | Turn the mistakes you keep correcting into agent skills | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`turn-my-request-into-a-checklist`](https://vidbyte.pro/agents/skills/turn-my-request-into-a-checklist) | Turn your request into a checklist before the work starts | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`vibe-coded-to-production`](https://vidbyte.pro/agents/skills/vibe-coded-to-production) | Make a vibe-coded app production ready | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |
| [`what-else-needs-doing`](https://vidbyte.pro/agents/skills/what-else-needs-doing) | Find what else needs doing after your coding agent finishes | 0.3.2, needs the next vidbyte-cli release (not on PyPI yet) |
| [`which-pr-to-review-first`](https://vidbyte.pro/agents/skills/which-pr-to-review-first) | Decide which open pull requests to review first | 0.3.0, needs the next vidbyte-cli release (not on PyPI yet) |

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
vidbyte-cli research start "your question"  # start a hosted Research harness run
vidbyte-cli research threads                # list your threads
```

The step-by-step guide lives at <https://vidbyte.pro/docs/cli/quickstart>, and
<https://vidbyte.pro/docs/cli/run-your-first-agent> runs a first agent.

## What runs where

| Agent | Runs on | Price | Command |
| --- | --- | --- | --- |
| [Persistent Researcher](https://vidbyte.pro/agents/persistent-researcher) (Research harness) | Vidbyte's servers | Usage-based, capped per run ($5.00 by default) | `research start` |
| [Ensemble Agent](https://vidbyte.pro/agents/ensemble-agent) | Your machine, through Codex | 2¢ per launch | `runtime same-host-ensemble` |
| [Persistent Agent](https://vidbyte.pro/agents/persistent-agent) | Your machine, through Codex | 2¢ per launch | `runtime persistence` |
| [Task Board](https://vidbyte.pro/agents/task-board) | Your machine, through Codex | 2¢ per run | `runtime task-board run` |
| [Stages Agent](https://vidbyte.pro/agents/stages-agent) | Your machine, through Codex | 1¢ per run | `runtime stages run` |
| [Suggestion Agent](https://vidbyte.pro/agents/suggestion-agent) | 0.2.0: your machine, on your own Codex sign-in. Later: Vidbyte's model gateway | Later: each model call at the model's rate plus 2%, which needs the next vidbyte-cli release (not on PyPI yet) | `agents suggest run` |

Every command in this table is in vidbyte-cli 0.2.0, the current PyPI release. The local
agents run their models on your own Codex or OpenAI account. All agents:
<https://vidbyte.pro/agents>.

### Research harness (hosted research threads)

Research runs on Vidbyte's servers. It is not a local skill, and the `/research` learning skill
is unrelated. `vidbyte-cli research start` starts a run of the
[Vidbyte Research harness](https://vidbyte.pro/docs/harnesses/research), and `research watch`
follows it. A run needs $5.00 spendable to start. It is billed by measured usage plus a 5¢ run
fee and a 25% margin, never past its cap (`--max-cost-cents`, $5.00 by default).

## Pricing

- `vidbyte-cli` and the agent skills are free and MIT-licensed.
- Hosted runs and local launch fees are paid from one prepaid Vidbyte API balance. Add to it at
  <https://vidbyte.pro/settings/api/usage>, or run `vidbyte-cli billing top-up --confirm`
  ($5.00 minimum, through x402).
- Research packs add one-time credit, from $29: <https://vidbyte.pro/harnesses/research>.
- Any Vidbyte account, the free plan included, can create an API key at
  <https://vidbyte.pro/settings/api>.

## Part of Vidbyte

- [Vidbyte SDK](https://vidbyte.pro/docs/sdks): `vidbyte-sdk`, a free MIT-licensed Python
  library for building, evaluating, and tracing agents.
- [JevAgent](https://vidbyte.pro/docs/jev-agent): pairs your agent with Jev, TypeSafe's
  decision model.
- [API pricing](https://vidbyte.pro/docs/pricing): what the Vidbyte API and its models cost.
- [Vidbyte learning skills](https://vidbyte.pro/blog/introducing-vidbyte-learning-skills): the
  npm package `vidbyte-skills`, separate from the agent skills in this repository.

## Commands

| Command | Purpose |
| --- | --- |
| `vidbyte-cli login` / `logout` / `whoami` | Manage the stored Vidbyte API key |
| `vidbyte-cli research start\|add\|resume` | Start a hosted Research harness run, add a run to a thread, or continue a run |
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
