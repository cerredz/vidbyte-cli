# Vidbyte

Set up [Vidbyte](https://vidbyte.pro) from your coding agent. The plugin's `setup` skill checks
whether `vidbyte-cli` is installed and logged in, installs it only after you say yes, walks you
through getting an API key without ever asking you to paste it into the chat, and verifies the
account before any Vidbyte agent runs. Setup itself spends nothing.

The skill needs a shell on your own machine, so it works in Claude Code, the Codex CLI and app,
and other terminal agents. In a chat with no shell, it gives you the install command to run
yourself.

## Install

Claude Code:

```bash
claude plugin install vidbyte --marketplace cerredz/vidbyte-cli
```

Codex:

```bash
codex plugin marketplace add cerredz/vidbyte-cli
codex plugin add vidbyte@vidbyte
```

## Try it

- "Set me up on Vidbyte."
- "Install vidbyte-cli and log me in."
- "Is my Vidbyte setup ready to run an agent?"

In Claude Code you can also run the skill directly with `/vidbyte:setup`.

## What it runs, sends, and fetches

The skill only gives your agent instructions. Everything it does happens through commands your
agent runs on your machine, and it asks before each install:

- **Installs** `vidbyte-cli` from [PyPI](https://pypi.org/project/vidbyte-cli/) with
  `pipx install vidbyte-cli` (or `pip` inside a virtual environment), and the Codex CLI only if
  you pick an agent that needs it. Each install waits for your yes.
- **Contacts Vidbyte.** `vidbyte-cli whoami`, `vidbyte-cli login`, and `vidbyte-cli runtime list`
  call the Vidbyte API at `https://vidbyte-backend.onrender.com` to check your key and read the
  runtime catalog and prices. `vidbyte-cli runtime doctor` checks your machine only.
- **Contacts OpenAI** only for agents that drive Codex: `vidbyte-cli provider whoami openai`
  verifies the OpenAI key you stored with `vidbyte-cli provider login openai`.
- **Never handles your keys in the chat.** You run `vidbyte-cli login` yourself; it reads the key
  at a hidden prompt and stores it in your operating system keyring.
- **Sends you to two pages** when they are missing: Google sign-in at `https://vidbyte.pro/login`
  and API key creation at `https://vidbyte.pro/settings/api`.

When you later run the Suggestion Agent, its goal, context, files, attachments, and project
memory travel through Vidbyte to OpenAI. The skill tells you this before your first run.

## Privacy, support, and license

- Privacy policy: <https://vidbyte.pro/privacy>
- Support: open an issue at <https://github.com/cerredz/vidbyte-cli/issues> or use
  <https://vidbyte.pro/contact>.
- License: MIT, see [LICENSE](LICENSE).
