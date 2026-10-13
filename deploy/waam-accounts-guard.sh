#!/bin/bash


REPO="${WAAM_REPO:-/srv/waam/waam-pipeline}"
ACC="$REPO/.waam-accounts.json"
BK="${WAAM_BACKUP_DIR:-/srv/waam/backups}"

count() {
  python3 -c 'import sys, json
try:
    print(len(json.load(sys.stdin).get("accounts", [])))
except Exception:
    print(0)' 2>/dev/null
}

live=0
if [ -f "$ACC" ]; then live=$(count < "$ACC"); fi
[ -z "$live" ] && live=0


if [ "$live" -gt 1 ] 2>/dev/null; then exit 0; fi

shopt -s nullglob
for f in "$BK"/waam-state-*.tar.gz "$BK"/waam-state-*.tgz; do
  c=$(tar -xzOf "$f" .waam-accounts.json 2>/dev/null | count)
  [ -z "$c" ] && c=0
  if [ "$c" -gt "$live" ] 2>/dev/null; then
    message="Possible account-store loss: $live account(s); $(basename "$f") has $c. Manual review required; no files changed."
    logger -t waam-accounts-guard "$message" 2>/dev/null || printf '%s\n' "$message" >&2
    break
  fi
done
exit 0
