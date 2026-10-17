---
name: setup
description: Use when the user wants to start using Vidbyte from this coding agent - "set me up on Vidbyte", "install vidbyte-cli and log me in", "is my Vidbyte setup ready?". Checks what is already installed, installs vidbyte-cli only after the user says yes, gets the user logged in without the API key ever entering the chat, and verifies the account before any Vidbyte agent runs. Needs a shell on the user's own machine.
metadata:
  commands: "--version, whoami, login, runtime doctor, runtime list, provider whoami, provider login"
  cost: "none - the setup commands start no paid run"
  writes-files: "false"
  min-cli-version: "0.2.0"
  status: stable
---

# Set up Vidbyte

The user wants to use Vidbyte (https://vidbyte.pro) from this coding agent. Vidbyte runs
specialized agents: some on Vidbyte's servers, some on the user's machine through their installed
coding agent, and the Suggestion Agent, which the user starts from `vidbyte-cli` while Vidbyte runs
its model calls. All of them go through one command-line tool, `vidbyte-cli`. This skill takes the
user from nothing to a verified setup, and it spends nothing.

Read this whole file before you start. Then run the checks in Step 1 yourself, before you ask the
user anything, and let what they print decide what you say next. Only two setup steps need the
user in a browser: Google sign-in and creating an API key. Send the user only to the ones the
checks show are missing, one link at a time, and wait for them to confirm each one.

Never ask the user to paste an API key into this conversation.

This skill needs a shell on the user's own machine. If you cannot run shell commands here, tell
the user that, give them the install command from Step 2A to run themselves, and stop.

## What the user needs

One Vidbyte API key is the only required thing. It is the user's identity, it owns everything they
create, and usage is billed against it. There is no path that skips it. For the Suggestion Agent,
the key must carry the `models:invoke` permission; an admin or full-access key also works.

Usage is paid from a prepaid balance on the Vidbyte account. Vidbyte reserves from that balance
before an agent starts, so a run the balance cannot cover is refused before it starts and charges
nothing. The Suggestion Agent reserves before each model call instead; if the balance runs out
partway, it stops and returns the ideas it has already reviewed.

## Step 1: check what is already set up

Run these and keep what each one prints:

    vidbyte-cli --version
    vidbyte-cli whoami
    vidbyte-cli runtime doctor

## Step 2: follow the branch the output picks

A. The CLI. If `vidbyte-cli --version` printed a version, tell the user in one line and go to B.
   If the command was not found, say: "It looks like vidbyte-cli isn't installed yet. Do you want me to install it for you?"
   - If the user says yes: install it from PyPI, then re-run `vidbyte-cli --version` and go to B.

         pipx install vidbyte-cli

     If pipx is not available, run `python -m pip install vidbyte-cli` inside a virtual
     environment instead.

   - If the user says no: tell them the CLI is the one thing every Vidbyte agent needs, and stop.

B. The login. If `vidbyte-cli whoami` printed "Authenticated as", tell the user which account and go to C.
   If it printed "Authentication is required.", ask: "You're not logged in yet. Do you already have a Vidbyte API key?"
   - If the user says yes: ask them to run `vidbyte-cli login` in their own terminal, not through
     you. It takes the key at a hidden prompt, or from stdin with `--with-token`, checks it against
     Vidbyte, and stores it in the operating system keyring. There is deliberately no `--api-key`
     flag, because anything in argv leaks into process listings, shell history, and CI logs.
     Re-run `vidbyte-cli whoami` when they say they are done.
   - If the user says no: ask whether they have a Vidbyte account.
     - No account: send them to https://vidbyte.pro/login and have them sign in with Google. The
       first sign-in creates the account; there is no separate registration form. Wait for them.
     - Then send them to https://vidbyte.pro/settings/api to create a key. If the page says the
       account cannot create a key, tell the user exactly what the page says and stop. The
       plaintext `vb_live_...` value is shown exactly once, so tell them to put it straight into
       their secret store and to rotate it if they ever lose it. Key creation is browser-only; no
       command or API route mints a key. Wait for them, then have them run `vidbyte-cli login`
       themselves as above.

C. The balance. The CLI has no command that reads the balance, and setup spends nothing, so do
   not ask the user about their balance now. If a later run exits with status 5, the balance could
   not cover it and nothing was charged: stop, show the user the CLI's message as printed, and
   wait for them.

D. The host, only if the user picked an agent that drives a coding agent on their machine. Skip D
   for the Suggestion Agent, which needs no Codex and no OpenAI key; remind the user instead that
   its model calls need a key with the `models:invoke` permission. Otherwise read the
   `vidbyte-cli runtime doctor` output.
   - If the codex line says "not found", say: "That agent runs through the Codex CLI, and this
     machine doesn't have it. Do you want me to install Codex?" Install it only if the user says yes.
   - Then run `vidbyte-cli provider whoami openai`. If it fails, say: "Codex needs an OpenAI key
     and none is stored. Please run `vidbyte-cli provider login openai` in your own terminal."
     Wait for them, then run it again.

## Step 3: verify, then tell the user what you found

    vidbyte-cli whoami            # proves the key is live
    vidbyte-cli runtime list      # the runtimes Vidbyte will admit, with their prices

Report what every check returned, in one short list, before anything is spent.

## Prices

Name a price only as `vidbyte-cli runtime list` prints it; never quote one from memory, because
prices change. For a runtime that drives a coding agent on the user's machine, that price is
Vidbyte's charge and not the whole cost: the model usage runs against the user's own provider
account, so say the two separately.

The Suggestion Agent buys no admission and uses no provider account of the user's. Each of its
model calls goes through Vidbyte and is billed to the Vidbyte balance by the tokens it uses, so
its cost follows the model, the rounds, and how much context the user sends. Its goal, context,
files, attachments, and project memory travel through Vidbyte to OpenAI; tell the user that before
their first Suggestion Agent run.

## Exit codes

| Exit | Meaning | What to do |
|---|---|---|
| 2 | The command was not accepted as typed | Check it against `vidbyte-cli <command> --help` and fix it. |
| 4 | Not logged in, or the key was refused | Go back to Step 2B. |
| 5 | The balance could not cover the run; nothing was charged | Stop, show the CLI's message as printed, and wait for the user. |

## Guardrails

- Never ask for, echo, or pass any API key, Vidbyte or provider, in this chat or on a command line.
- Install software (vidbyte-cli, pipx, Codex) only after the user says yes.
- Setup spends nothing. Before any later command that charges the balance, tell the user the price
  from `vidbyte-cli runtime list` and wait for a yes.
- Never run `vidbyte-cli billing top-up --confirm` on your own; it spends real money.

Reference docs: https://vidbyte.pro/docs/cli/run-your-first-agent
