#!/usr/bin/env bash
# Fails if any file git tracks (or would track) contains a string shaped like a credential.
# Usage: scripts/check_secrets.sh [file ...]   (default: tracked + untracked-but-not-ignored files under cwd)
#
# Findings are reported as file:line only. Printing the matched text would copy a real secret
# into terminal scrollback, CI logs and evidence files.
# Exit status: 0 = clean, 1 = findings, 2 = the scan itself failed.
set -euo pipefail

PATTERN='sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

files=()
if [ "$#" -gt 0 ]; then
  files=("$@")
else
  # Skip index entries whose file was deleted from the working tree.
  while IFS= read -r f; do
    if [ -e "$f" ]; then files+=("$f"); fi
  done < <(git ls-files --cached --others --exclude-standard)
fi

if [ "${#files[@]}" -eq 0 ]; then
  echo "no files to check" >&2
  exit 2
fi

set +e
matches=$(grep -HnE "$PATTERN" -- "${files[@]}")
status=$?
set -e

if [ "$status" -gt 1 ]; then
  echo "scan failed: grep exited with status $status" >&2
  exit 2
fi
if [ "$status" -eq 0 ]; then
  echo "secret-like strings found at (matched text not shown):" >&2
  printf '%s\n' "$matches" | cut -d: -f1,2 >&2
  exit 1
fi
echo "no secret-like strings in ${#files[@]} files"
