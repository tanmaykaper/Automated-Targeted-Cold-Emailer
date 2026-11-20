#!/usr/bin/env bash
# Commit bot state and push with rebase-and-retry, so concurrent pushes (a human commit, another run)
# never lose a tick's work. State files are line-oriented JSONL/CSV, so rebases resolve cleanly.
set -uo pipefail

MSG="${1:-bot: state update}"
PATHS=(state inputs)

git config user.name  "outbound-bot[actions]"
git config user.email "outbound-bot@users.noreply.github.com"

git add -A "${PATHS[@]}"
if git diff --cached --quiet; then
  echo "No state changes to commit."
  exit 0
fi
git commit -q -m "$MSG"

for attempt in 1 2 3 4 5; do
  if git push -q origin HEAD:"${GITHUB_REF_NAME:-main}"; then
    echo "Pushed state on attempt $attempt."
    exit 0
  fi
  echo "Push rejected (attempt $attempt); rebasing onto remote…"
  git fetch -q origin "${GITHUB_REF_NAME:-main}"
  if ! git rebase -q "origin/${GITHUB_REF_NAME:-main}"; then
    # Same-line conflict in a state file: keep the bot's version (it is the newer reconciliation).
    git checkout --theirs -- "${PATHS[@]}" 2>/dev/null || true
    git add -A "${PATHS[@]}"
    GIT_EDITOR=true git rebase --continue || { git rebase --abort; echo "::error::rebase failed"; exit 1; }
  fi
  sleep $(( attempt * 3 + RANDOM % 5 ))
done
echo "::error::Could not push state after 5 attempts"
exit 1
