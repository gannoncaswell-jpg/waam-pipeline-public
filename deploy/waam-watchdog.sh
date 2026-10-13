#!/bin/bash


MARKER=/run/waam-workbench-down

if [ -x /usr/local/sbin/waam-accounts-guard.sh ]; then
    sudo -u waam /usr/local/sbin/waam-accounts-guard.sh || true
fi

alert() { sudo -u waam bash -c "cd /srv/waam/waam-pipeline && PYTHONPATH=src /srv/waam/venv/bin/python /srv/waam/waam-alert.py \"$1\" \"$2\"" 2>&1 | logger -t waam-watchdog; }
healthy() {
  systemctl is-active --quiet waam-workbench || return 1
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8017/ 2>/dev/null || echo 000)
  [ "$code" != "000" ]
}
if healthy; then
  if [ -f "$MARKER" ]; then rm -f "$MARKER"; alert "WAAM workbench RECOVERED" "waam-workbench is active and answering again."; fi
else
  echo "WATCHDOG: workbench down - reset-failed + start"
  systemctl reset-failed waam-workbench 2>/dev/null || true
  systemctl start waam-workbench
  if [ ! -f "$MARKER" ]; then
    touch "$MARKER"; sleep 8
    if healthy; then M="waam-workbench was down; the watchdog restarted it and it is answering again."; else M="waam-workbench is DOWN and the watchdog could NOT bring it back. A human needs to look."; fi
    alert "WAAM workbench DOWN" "$M"
  fi
fi
