#!/usr/bin/env bash
# The Task Board's --on-checkpoint hook. The board runs it after every step and waits for it to
# finish, from the board's checkpoint directory, before the next issue's agent starts. It saves
# this issue's changes as one commit on its own branch, then resets the worktree to the base
# commit, so every issue starts from a clean tree and every branch holds exactly one issue.
# The board sets STEP_INDEX and STEP_STATUS. Never run this by hand.
set -euo pipefail
# The board discards the hook's output, so keep its errors where the Deliver step can read them.
exec 2>>../../hook.log
cd "$(git rev-parse --show-toplevel)"
STATE=.vidbyte/issue-tasks
. "$STATE/run.env"

# Act only inside the dedicated worktree prepare.sh created, never in a main checkout.
if [ "$(git rev-parse --show-toplevel)" != "$WORKTREE" ] ||
  [ "$(git rev-parse --git-dir)" = "$(git rev-parse --git-common-dir)" ]; then
  echo "on_step.sh: refusing to run outside the worktree $WORKTREE" >&2
  exit 1
fi

# Line N+1 of issues.tsv is board step N, because run.sh passes the task files in that order.
TAB="$(printf '\t')"
LINE="$(sed -n "$((STEP_INDEX + 1))p" "$STATE/issues.tsv")"
NUMBER="${LINE%%"$TAB"*}"
TITLE="${LINE#*"$TAB"}"
if [ -z "$NUMBER" ]; then
  echo "on_step.sh: no issue for step $STEP_INDEX" >&2
  exit 1
fi

# Plumbing, not git commit: it finishes in well under the hook's 60-second limit and never runs
# the repository's commit hooks. The project's checks run before anything is pushed.
git add -A
TREE="$(git write-tree)"
if [ "$TREE" = "$(git rev-parse "$BASE^{tree}")" ]; then
  SAVED="no changes"
else
  COMMIT="$(printf 'Implement #%s: %s\n\nVidbyte Task Board %s, step %s (%s).\nhttps://github.com/%s/issues/%s\n' \
    "$NUMBER" "$TITLE" "$BOARD_ID" "$STEP_INDEX" "$STEP_STATUS" "$REPO" "$NUMBER" |
    git commit-tree "$TREE" -p "$BASE")"
  git update-ref "refs/heads/$PREFIX$NUMBER" "$COMMIT"
  SAVED="$PREFIX$NUMBER $COMMIT"
fi

# Detach first so the reset can never move a branch, then drop every change and untracked file.
# .vidbyte/issue-tasks/ is in info/exclude, so git clean leaves the board's state alone.
git checkout --quiet --force --detach "$BASE"
git reset --quiet --hard "$BASE"
git clean -fdq
printf '%s\t%s\t%s\t%s\n' "$STEP_INDEX" "$NUMBER" "$STEP_STATUS" "$SAVED" >>"$STATE/steps.tsv"
