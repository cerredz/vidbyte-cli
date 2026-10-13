# vidbyte-cli troubleshooting

<!--
Canonical copy. Every public skill in cerredz/vidbyte-cli carries this file at
skills/<slug>/references/troubleshooting.md. Edit this copy, then regenerate every public copy.
Every exit code, error code, field name, and quoted message was checked against
the vidbyte-cli command contract on main (contract commit 9703b53) on 2026-10-11.
-->

Read this file when a `vidbyte-cli` command exits with anything other than 0. Always run commands
with the root options before the command, `vidbyte-cli --json --no-input <command> ...`, so the
failure arrives as one JSON document you can branch on instead of styled text.

## How a failure looks

stdout carries only results; progress and prompts go to stderr. Every JSON document is
`{"schema_version": 1, "kind": ..., "data": ...}`. A failure is `kind: "error"`, and its `data`
holds:

| Field | Meaning |
|---|---|
| `code` | A stable error code from the table below. Branch on this, not on the message. |
| `message` | One sentence saying what went wrong. Quote it to the user. |
| `description` | Why it happened and what was or was not charged. Read it before acting. |
| `hint` | The next command or action to take, when there is one. |
| `exit_code` | The same number the process exited with. |
| `retryable` | `true` only when the same command may succeed if run again later. |
| `request_id` | Present when the Vidbyte API answered; give it to the user if they contact support. |
| `remediation` | Present on credit-exhausted errors; see "Out of credits" below. |

## Exit codes

| Exit | Meaning | What the skill does |
|---|---|---|
| 0 | Success | Read the result document. |
| 1 | Operational failure (network, API, keyring, or an agent run that failed) | Read `message` and `hint`. Retry only if `retryable` is `true`, and never in a tight loop. |
| 2 | Usage error (a bad flag or value, or a missing confirmation) | Fix the command. Do not retry it unchanged. |
| 3 | Partial outcome (the ensemble produced work it could not use or finish) | Show the user what was produced and ask how to continue. Do not rerun blindly. |
| 4 | Authentication (no key, a rejected key, or a missing scope) | Stop and follow `references/setup.md` step 2. |
| 5 | Credit exhausted | Stop. Follow "Out of credits" below. Never top up without the user's approval. |
| 70 | Internal error in the CLI | Stop and show the user `message` and `request_id`. |
| 130 | Interrupted | The user or the system stopped the run. Ask before starting it again. |

## Error codes

| `code` | Usual cause |
|---|---|
| `INVALID_ARGUMENT` | A flag value is outside its range or set, or required input is missing. |
| `AUTH_REQUIRED` | No Vidbyte key is stored, or the key was rejected or lacks a scope. |
| `CREDIT_EXHAUSTED` | The Vidbyte balance cannot fund this operation. |
| `API_UNAVAILABLE` | The Vidbyte API failed, timed out, or rate-limited the key. |
| `API_PROTOCOL_ERROR` | The API answered in a shape the CLI does not accept. Upgrade the CLI. |
| `CONFIG_INVALID` / `CONFIG_WRITE_CONFLICT` | A `vidbyte-cli config` value is wrong, or two writes raced. |
| `CREDENTIAL_STORE_UNAVAILABLE` | The OS keyring is missing or locked. |
| `OPERATION_FAILED` | A request or an agent run failed for a reason the message names. |
| `NOT_IMPLEMENTED` | The command exists but its agent is not built yet (for example `runtime adversarial-team`). |
| `INTERRUPTED` | The run was cancelled. |
| `INTERNAL_ERROR` | A bug in the CLI. |

## Not logged in (exit 4)

The message `Authentication is required.` comes with the hint `Run 'vidbyte-cli login' first.`
The message `The API key is missing the scope this command requires.` means the key exists but
lacks a scope; the user grants it in the Vidbyte dashboard. Both are fixed by
`references/setup.md` step 2, never by retrying.

`Login requires explicit token input in noninteractive mode.` (exit 2) means login ran without a
terminal. Its hint is `Pipe the token to 'vidbyte-cli login --with-token'.`

## Out of credits (exit 5)

The message is `The Vidbyte account does not have enough API balance for this operation.` and the
hint is ``Get user approval, run `vidbyte-cli billing top-up --confirm`, verify the credited
balance, then retry the original operation.``

1. Stop. Do not retry the operation and do not send more turns.
2. Read `data.remediation`. Its fields are `action` (`top_up_api_balance`),
   `requires_user_approval`, `topup_method`, `topup_path`, `cli_command`
   (`vidbyte-cli billing top-up --confirm`), `minimum_topup_cents`,
   `supported_payment_methods` (`x402`, `mpp`), `browser_url`, `retry_original_operation`, and
   `steps`.
3. When `requires_user_approval` is `true`, which is the normal case, ask the user in plain words:
   what failed, that topping up spends real money, and the two ways to do it. Wait for an explicit
   yes before spending anything.
4. Browser path: send the user to `browser_url` to add balance themselves.
5. CLI path, only after an explicit yes: `vidbyte-cli --json billing top-up --confirm`. It pays a
   fixed $5.00 through x402 from the wallet in `VIDBYTE_X402_PRIVATE_KEY` on
   `VIDBYTE_X402_NETWORK` (Base, `eip155:8453`, by default). The wallet must already hold USDC; the
   CLI never funds it. The generated idempotency key is printed to stderr before paying, so a lost
   response can be recovered with `--idempotency-key` instead of paying twice.
6. Confirm the `billing.top_up` result shows `credited_cents` and an `available_balance_cents` that
   covers the original operation. Then rerun the original command, reusing its own
   `--idempotency-key` if it had one.

Without `--confirm`, top-up refuses with `Confirm before purchasing Vidbyte API balance.`
(exit 2). That refusal is the guardrail working; never add `--confirm` on your own.

If the failure came from the MCP server's `vidbyte_plan` tool instead, the hint is
`Add balance at https://vidbyte.pro/usage, then call vidbyte_plan again.`

## Rate limited (exit 1, retryable)

The message is `The Vidbyte API request budget for this key is exhausted.` The budget is a
weighted per-minute allowance per key, and starting a research run uses far more of it than
reading one. Wait the number of seconds the hint gives, or a minute when it gives none, and poll
status no faster than every 10 seconds.

## Idempotency keys

Priced commands that buy an admission accept `--idempotency-key` (`agents suggest run` buys none,
so it has no key: each of its model calls is billed on its own). The key is one string of 8 to 128 letters, digits, dots,
underscores, colons, or hyphens (`[A-Za-z0-9._:-]{8,128}`). Reusing the same key recovers an
admission whose response was lost without paying twice. It does not resume local work, and it
must never contain account data or secrets.

## Finding a run's id

Agent runs are recorded in a local ledger. Read it offline and for free:

```bash
vidbyte-cli --json runs last --agent <agent-id>
vidbyte-cli --json runs list --agent <agent-id>
vidbyte-cli --json runs show <id-or-prefix>
```

The `--agent` ids are `agents.suggest`, `runtime.persistence`, `runtime.same-host-ensemble`,
`runtime.task-board`, `runtime.stages`, and `agents.bug-finder`. Research runs are not in this
ledger; they print their own `run_id`, which `vidbyte-cli research status <run_id>` and
`vidbyte-cli research watch <run_id>` read.

## Resuming long work

| Agent | How to continue after a failure |
|---|---|
| Persistent Researcher | `vidbyte-cli research status <run_id>`; when it reports `resumable`, `vidbyte-cli research resume <run_id>`. |
| Task Board | Rerun `vidbyte-cli runtime task-board run` with `--checkpoint-id <id>` and `--retry-failed-only` or `--resume-from <N>`. |
| Persistence, Ensemble, Stages, Suggestion | No resume. Ask the user before starting a new run, which is a new charge. |
