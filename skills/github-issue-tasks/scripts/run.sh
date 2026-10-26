#!/usr/bin/env bash
# The one paid step: a Vidbyte Task Board run (2 cents + OpenAI API usage on the user's own OpenAI
# API key, which needs credits) with one fresh Codex agent per issue, in the worktree prepare.sh
# created. It is also the only way to retry or resume the
# board, because only this command attaches the on_step.sh hook that keeps each issue on its own
# branch; the resume_command the CLI prints does not.
# Usage: WORKTREE=<path prepare.sh printed> bash <skill>/scripts/run.sh
# Optional: MAX_TOKENS=<n> to stop starting new issues after that many reported tokens,
#           RETRY_FAILED=1 to re-run only the failed issues, FROM=<n> to continue from step n,
#           KEY=<earlier key> only to recover that same admission after a lost response.
set -uo pipefail
cd "${WORKTREE:?Set WORKTREE to the path prepare.sh printed.}" || exit 1
STATE=.vidbyte/issue-tasks
. "$STATE/run.env"

# Each issue must start from the base commit with nothing left over from an earlier step.
if [ "$(git rev-parse HEAD)" != "$BASE" ] || [ -n "$(git status --porcelain)" ]; then
  echo "The worktree is not clean at the base commit $BASE. Show the user 'git -C \"$WORKTREE\" status' and ask before changing anything." >&2
  exit 1
fi

# A retry or resume reuses the task list the board stored; a new board lists one file per issue,
# in issues.tsv order, which is the order on_step.sh maps step numbers back to issues.
ARGS=()
if [ -n "${RETRY_FAILED:-}" ]; then
  ARGS+=(--retry-failed-only)
elif [ -n "${FROM:-}" ]; then
  ARGS+=(--from "$FROM")
else
  while IFS="$(printf '\t')" read -r NUMBER _; do
    ARGS+=(--task-file "$STATE/tasks/$NUMBER.md")
  done <"$STATE/issues.tsv"
fi
if [ -n "${MAX_TOKENS:-}" ]; then
  ARGS+=(--max-tokens "$MAX_TOKENS")
fi
KEY="${KEY:-github-issue-tasks:$(date -u +%Y%m%d):$(date -u +%H%M%S)$RANDOM}"

# Keep every board setting identical across runs: a retry or resume is refused before payment
# when the stored settings differ.
vidbyte-cli --json --no-input runtime task-board run \
  "${ARGS[@]}" \
  --context-mode isolated \
  --no-stop-on-error \
  --retries-per-task 0 \
  --turn-timeout 7200 \
  --checkpoint-root "$STATE/boards" \
  --checkpoint-id "$BOARD_ID" \
  --report-file "$STATE/report.md" \
  --on-checkpoint "bash ../../on_step.sh" \
  --idempotency-key "$KEY" >"$STATE/result.json"
STATUS=$?
echo "vidbyte-cli exited $STATUS; idempotency key $KEY; result in $WORKTREE/$STATE/result.json"
exit "$STATUS"
