#!/usr/bin/env bash


set -euo pipefail


if [ -t 1 ]; then
    _b=$'\033[1m'; _d=$'\033[2m'; _o=$'\033[0m'
else
    _b=""; _d=""; _o=""
fi

phase()  { printf '\n%s==> %s%s\n' "$_b" "$*" "$_o"; }
note()   { printf '    %s\n' "$*"; }
detail() { printf '    %s%s%s\n' "$_d" "$*" "$_o"; }
warn()   { printf '    WARNING: %s\n' "$*" >&2; }

die() {
    printf '\n%sFAILED:%s %s\n' "$_b" "$_o" "$1" >&2
    shift
    for line in "$@"; do printf '    %s\n' "$line" >&2; done
    exit 1
}


SERVICE="${WAAM_SERVICE:-waam-workbench}"
VENV="${WAAM_VENV:-/srv/waam/venv}"
BACKUP_DIR="${WAAM_BACKUP_DIR:-/srv/waam/backups}"


KEEP_BACKUPS="${WAAM_KEEP_BACKUPS:-10}"
PORT="${WAAM_PORT:-8017}"
RUN_LOGS_ENV="${WAAM_RUN_LOGS_ENV:-/etc/waam/run-logs.env}"
DO_TESTS=1
DO_RESTART=1
DRY_RUN=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)     DRY_RUN=1 ;;
        --skip-tests)  DO_TESTS=0 ;;
        --no-restart)  DO_RESTART=0 ;;
        --yes|-y)      ASSUME_YES=1 ;;
        --service)     SERVICE="${2:?--service needs a unit name}"; shift ;;
        --venv)        VENV="${2:?--venv needs a path}"; shift ;;
        --backup-dir)  BACKUP_DIR="${2:?--backup-dir needs a path}"; shift ;;
        --keep-backups) KEEP_BACKUPS="${2:?--keep-backups needs a count}"; shift ;;
        --port)        PORT="${2:?--port needs a number}"; shift ;;
        -h|--help)     sed -n '3,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             die "unknown option: $1" "Run 'deploy/update.sh --help'." ;;
    esac
    shift
done


phase "1/8  Preconditions"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SCRIPT_DIR/.." && pwd)"
detail "repo:    $REPO"
detail "venv:    $VENV"
detail "service: $SERVICE"

[ -f "$REPO/src/waam_pipeline/server.py" ] || die \
    "$REPO does not look like the waam-pipeline checkout." \
    "Expected src/waam_pipeline/server.py underneath it." \
    "Run this script from inside the checkout: <checkout>/deploy/update.sh"

[ -d "$REPO/.git" ] || die \
    "$REPO is not a git checkout, so there is nothing to pull into." \
    "A copied directory cannot be updated safely -- re-clone it," \
    "docs/vm-server-setup.md section 7."

if [ "$(id -u)" = "0" ]; then
    die "do not run this as root." \
        "The checkout is owned by the service user, and git refuses to read a" \
        "repository owned by somebody else ('detected dubious ownership')." \
        "/api/save shells out to git to stamp the commit into every emitted" \
        "job, and that failure is swallowed -- so the symptom would be jobs" \
        "quietly losing their provenance, not an error." \
        "Run it as the service user instead:" \
        "  sudo -u waam $REPO/deploy/update.sh"
fi

OWNER="$(stat -c '%U' "$REPO")"
ME="$(id -un)"
if [ "$OWNER" != "$ME" ]; then
    die "$REPO is owned by '$OWNER' but you are '$ME'." \
        "git will refuse this repository, and the job provenance stamp fails" \
        "silently when it does. Either run as $OWNER, or fix the ownership:" \
        "  sudo chown -R $ME:$ME $REPO"
fi

VPY="$VENV/bin/python"
[ -x "$VPY" ] || die \
    "no python at $VPY" \
    "Create the virtualenv first (docs/vm-server-setup.md section 6):" \
    "  python3 -m venv $VENV" \
    "  $VENV/bin/pip install numpy"


IMPORT_LOG="$(mktemp)"
if ! (cd "$REPO" && PYTHONPATH=src "$VPY" -c 'import waam_pipeline.server') \
        >"$IMPORT_LOG" 2>&1; then
    tail -5 "$IMPORT_LOG" | sed 's/^/    /' >&2
    rm -f "$IMPORT_LOG"
    die "$VPY cannot import waam_pipeline.server (traceback above)." \
        "Most often numpy is missing from the venv:" \
        "  $VENV/bin/pip install numpy"
fi
rm -f "$IMPORT_LOG"
note "$("$VPY" --version), imports waam_pipeline.server cleanly"

command -v git >/dev/null 2>&1 || die "git is not on PATH." "  sudo apt install git"

if [ "$DO_RESTART" -eq 1 ]; then
    command -v systemctl >/dev/null 2>&1 || die \
        "systemctl is not on PATH, so the service cannot be restarted." \
        "Re-run with --no-restart and restart it however this box runs it."
    systemctl cat "$SERVICE" >/dev/null 2>&1 || die \
        "there is no systemd unit named '$SERVICE'." \
        "Install it (docs/vm-server-setup.md section 8):" \
        "  sudo install -m 0644 $REPO/deploy/waam-workbench.service \\" \
        "       /etc/systemd/system/waam-workbench.service" \
        "  sudo systemctl daemon-reload" \
        "  sudo systemctl enable --now waam-workbench" \
        "Or pass --service <name> if yours is called something else."
fi


phase "2/8  Back up the state that git cannot restore"


STATE_ITEMS="saves .waam-accounts.json .waam-webauthn.json .waam-auth.json .waam-tokens.json .waam-token .waam-access.log"

PRESENT=""
for item in $STATE_ITEMS; do
    [ -e "$REPO/$item" ] && PRESENT="$PRESENT $item"
done
PRESENT="${PRESENT# }"

STAMP=""
ARCHIVE=""
if [ -z "$PRESENT" ]; then
    note "nothing to back up yet (no accounts, no saved jobs, no log)"
elif [ "$DRY_RUN" -eq 1 ]; then
    detail "would archive:$PRESENT -> $BACKUP_DIR/waam-state-<timestamp>.tar.gz"
else
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    ARCHIVE="$BACKUP_DIR/waam-state-$STAMP.tar.gz"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    # shellcheck disable=SC2086
    tar -czf "$ARCHIVE" -C "$REPO" $PRESENT
    chmod 600 "$ARCHIVE"


    tar -tzf "$ARCHIVE" >/dev/null 2>&1 || die \
        "the backup at $ARCHIVE will not list back." \
        "Refusing to update on top of an unverified backup." \
        "Check free space:  df -h $BACKUP_DIR"
    note "backed up -> $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
    detail "contains:$PRESENT"
    warn "that archive holds scrypt password hashes plus the SMTP app password"
    warn "and any OAuth client secret from .waam-auth.json. Keep it mode 0600,"
    warn "off shared drives, and out of git."
fi


if [ "$DRY_RUN" -eq 1 ]; then
    detail "would keep the newest $KEEP_BACKUPS archives in $BACKUP_DIR"
elif [ -d "$BACKUP_DIR" ]; then
    case "$KEEP_BACKUPS" in
        ''|*[!0-9]*) die "--keep-backups needs a whole number, not '$KEEP_BACKUPS'." ;;
    esac
    [ "$KEEP_BACKUPS" -ge 1 ] || die \
        "--keep-backups must be at least 1." \
        "Deleting every backup is not something this script will do."
    STALE="$(ls -1t "$BACKUP_DIR"/waam-state-*.tar.gz "$BACKUP_DIR"/waam-state-*.tgz \
             2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" || true)"
    if [ -n "$STALE" ]; then
        GONE=0
        while IFS= read -r stale; do
            [ -n "$stale" ] || continue
            rm -f -- "$stale"
            GONE=$((GONE + 1))
        done <<STALE_LIST
$STALE
STALE_LIST
        note "pruned $GONE old backup(s); newest $KEEP_BACKUPS kept"
    fi
fi


phase "3/8  Pull"

BEFORE="$(git -C "$REPO" rev-parse HEAD)"
BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
detail "on $BRANCH at $(git -C "$REPO" rev-parse --short HEAD)"

DIRTY="$(git -C "$REPO" status --porcelain)"
if [ -n "$DIRTY" ]; then
    warn "the working tree has uncommitted changes:"
    printf '%s\n' "$DIRTY" | sed 's/^/        /' >&2
    warn "a fast-forward pull refuses rather than clobbering them."
fi

AFTER="$BEFORE"
if ! git -C "$REPO" remote | grep -q .; then
    warn "no git remote is configured on this checkout, so there is nothing to"
    warn "pull from. That is the state the project is in today -- see"
    warn "docs/vm-server-setup.md section 7, 'Getting the repo onto the box'."
    warn "Add one and this step starts working:"
    warn "  git -C $REPO remote add origin <url>"
    note "no pull attempted; the rest of this run still tests and verifies."
elif [ "$DRY_RUN" -eq 1 ]; then
    REMOTE="$(git -C "$REPO" remote | head -1)"
    detail "would run: git -C $REPO pull --ff-only $REMOTE $BRANCH"
else
    REMOTE="$(git -C "$REPO" remote | head -1)"
    git -C "$REPO" pull --ff-only "$REMOTE" "$BRANCH" || die \
        "git pull --ff-only failed." \
        "It refuses rather than merging, deliberately. Usually one of:" \
        "  - local commits or edits on this box -> commit or stash them" \
        "  - the remote branch was force-pushed -> re-clone; do not reset" \
        "Nothing was restarted. The running server is untouched and healthy."
    AFTER="$(git -C "$REPO" rev-parse HEAD)"
fi

if [ "$BEFORE" = "$AFTER" ]; then
    note "already up to date at $(git -C "$REPO" rev-parse --short HEAD)"
else
    note "$(git -C "$REPO" rev-parse --short "$BEFORE") -> $(git -C "$REPO" rev-parse --short "$AFTER")"
    git -C "$REPO" --no-pager diff --stat "$BEFORE" "$AFTER" | sed 's/^/    /'


    if git -C "$REPO" --no-pager diff --name-only "$BEFORE" "$AFTER" \
            | grep -qE '^(robots/|docs/cell-calibration\.json|examples/cell_)'; then
        warn "this update touched cell geometry or calibration."
        warn "The suite below is the only thing that checks it. Do not"
        warn "--skip-tests on this one."
    fi
fi


phase "4/8  Test"

if [ "$DO_TESTS" -eq 0 ]; then
    warn "skipping the suite (--skip-tests)."
    warn "It is the only thing between a bad pull and a job a robot will run."
elif [ "$DRY_RUN" -eq 1 ]; then
    detail "would run: WAAM_ACTIVITY_LOG=<tmp> WAAM_ACCESS_LOG=<tmp> PYTHONPATH=src $VPY -m unittest discover -s tests"
else
    TEST_LOG="$(mktemp)"


    SUITE_ACT="$(mktemp -u)"


    SUITE_ACC="$(mktemp -u)"
    if (cd "$REPO" && WAAM_ACTIVITY_LOG="$SUITE_ACT" WAAM_ACCESS_LOG="$SUITE_ACC" \
            PYTHONPATH=src "$VPY" -m unittest discover -s tests) \
            >"$TEST_LOG" 2>&1; then
        grep -E '^Ran [0-9]+ tests' "$TEST_LOG" | sed 's/^/    /' || true
        tail -1 "$TEST_LOG" | sed 's/^/    /'
        rm -f "$TEST_LOG" "$SUITE_ACT" "$SUITE_ACT".1 "$SUITE_ACC"
    else
        printf '\n'
        grep -E '^(FAIL|ERROR): ' "$TEST_LOG" | sed 's/^/    /' >&2 || true
        grep -E '^Ran [0-9]+ tests' "$TEST_LOG" | sed 's/^/    /' >&2 || true
        tail -1 "$TEST_LOG" | sed 's/^/    /' >&2
        rm -f "$SUITE_ACT" "$SUITE_ACT".1 "$SUITE_ACC"
        die "the suite is RED on the code that is now on disk." \
            "The server has NOT been restarted -- it is still serving the" \
            "previous code, which is exactly where you want to be." \
            "Full output: $TEST_LOG" \
            "Fix it, or -- if you have read every failure above and judged" \
            "each one pre-existing and unrelated -- re-run with --skip-tests."
    fi
fi


phase "5/8  Run-log catalog"


RUN_LOGS_DIR="$(dirname -- "$RUN_LOGS_ENV")"

if [ -d "$RUN_LOGS_DIR" ] && [ ! -x "$RUN_LOGS_DIR" ]; then
    die "$ME cannot search $RUN_LOGS_DIR, so it cannot tell whether run logs are on PostgreSQL." \
        "Make the directory searchable (workbench.env inside stays 0600):" \
        "  sudo chmod 0755 $RUN_LOGS_DIR"
fi
if [ ! -e "$RUN_LOGS_ENV" ]; then
    note "no $RUN_LOGS_ENV: run logs are not on PostgreSQL here, nothing to migrate"
else
    [ -r "$RUN_LOGS_ENV" ] || die \
        "$RUN_LOGS_ENV exists but $ME cannot read it." \
        "It holds no secret (peer authentication), so make it world-readable:" \
        "  sudo chmod 0644 $RUN_LOGS_ENV"
    RUN_LOG_DSN="$(sed -n 's/^WAAM_RUN_LOG_PG_DSN=//p' "$RUN_LOGS_ENV" | tail -n 1 | tr -d '\r')"
    case "$RUN_LOG_DSN" in
        \"*\") RUN_LOG_DSN="${RUN_LOG_DSN#\"}"; RUN_LOG_DSN="${RUN_LOG_DSN%\"}" ;;
        \'*\') RUN_LOG_DSN="${RUN_LOG_DSN#\'}"; RUN_LOG_DSN="${RUN_LOG_DSN%\'}" ;;
    esac
    [ -n "$RUN_LOG_DSN" ] || die \
        "$RUN_LOGS_ENV does not set WAAM_RUN_LOG_PG_DSN, or sets it empty." \
        "The service refuses run logs with an empty value. Set it, as in" \
        "deploy/run-logs-postgres.conf.example, or remove the file to keep" \
        "run logs on SQLite."
    case "$RUN_LOG_DSN" in
        *[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]*|*://*:*@*)
            die "$RUN_LOGS_ENV puts a password in a world-readable file." \
                "Use peer authentication (dbname=waam_runs over the socket), or keep a" \
                "password in /srv/waam/.pgpass (mode 0600), never in the DSN." ;;
    esac
    if command -v systemctl >/dev/null 2>&1 && systemctl cat "$SERVICE" >/dev/null 2>&1; then


        UNIT_ENV_FILES="$(systemctl show -p EnvironmentFiles "$SERVICE" 2>/dev/null || true)"
        case "$UNIT_ENV_FILES" in
            *"$RUN_LOGS_ENV"*) ;;
            *) die "$SERVICE does not load $RUN_LOGS_ENV, so its run logs would stay on SQLite." \
                   "Install the unit from this checkout, then re-run this script:" \
                   "  sudo install -m 0644 $REPO/deploy/waam-workbench.service /etc/systemd/system/" \
                   "  sudo systemctl daemon-reload" ;;
        esac
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        detail "would run: WAAM_RUN_LOG_PG_DSN=$RUN_LOG_DSN PYTHONPATH=src $VPY -m waam_pipeline.run_log_postgres migrate"
    else
        MIGRATE_LOG="$(mktemp)"


        if (cd "$REPO" && WAAM_RUN_LOG_PG_DSN="$RUN_LOG_DSN" PYTHONPATH=src \
                "$VPY" -m waam_pipeline.run_log_postgres migrate) >"$MIGRATE_LOG" 2>&1; then
            sed 's/^/    /' "$MIGRATE_LOG"
            rm -f "$MIGRATE_LOG"
        else
            sed 's/^/    /' "$MIGRATE_LOG" >&2
            rm -f "$MIGRATE_LOG"
            die "the run-log catalog migration failed (output above)." \
                "The server has NOT been restarted, but run logs refuse requests until" \
                "the schema matches this checkout. Fix the cause, then re-run this" \
                "script, or migrate by hand (docs/run-logs.md):" \
                "  cd $REPO && WAAM_RUN_LOG_PG_DSN='$RUN_LOG_DSN' PYTHONPATH=src \\" \
                "      $VPY -m waam_pipeline.run_log_postgres migrate"
        fi
    fi
fi


phase "6/8  Restart"

if [ "$DO_RESTART" -eq 0 ]; then
    warn "not restarting (--no-restart). The new code is on disk, but the"
    warn "running process still has the old modules loaded in memory."
elif [ "$DRY_RUN" -eq 1 ]; then
    detail "would run: sudo systemctl restart $SERVICE"
else


    if [ "$ASSUME_YES" -eq 0 ]; then
        if [ ! -t 0 ]; then
            die "a restart discards guest sessions and every unsaved slice," \
                "and this run has no terminal to ask on." \
                "Re-run with --yes if that is fine, or with --no-restart to" \
                "stage the code and restart it yourself at a quiet moment."
        fi
        printf '\n    A restart discards guest sessions and every unsaved slice.\n'
        printf '    Restart %s now? [y/N] ' "$SERVICE"
        read -r reply
        case "$reply" in
            [yY]|[yY][eE][sS]) ;;
            *) die "not restarting, at your request." \
                   "The new code is on disk. Finish it whenever you like:" \
                   "  sudo systemctl restart $SERVICE" ;;
        esac
    fi
    sudo systemctl restart "$SERVICE" || die \
        "sudo systemctl restart $SERVICE failed." \
        "  systemctl status $SERVICE" \
        "  journalctl -u $SERVICE -n 50 --no-pager"
    note "restarted"
fi


phase "7/8  Verify"

if [ "$DRY_RUN" -eq 1 ]; then
    detail "would poll http://127.0.0.1:$PORT/ until it answers"
elif [ "$DO_RESTART" -eq 0 ]; then
    detail "skipped -- nothing was restarted, so nothing changed to verify"
else
    systemctl is-active --quiet "$SERVICE" || die \
        "$SERVICE is not active after the restart." \
        "  systemctl status $SERVICE" \
        "  journalctl -u $SERVICE -n 50 --no-pager"
    note "unit is active"

    if ! command -v curl >/dev/null 2>&1; then
        warn "curl is not installed, so the HTTP check cannot run."
        warn "'active' is much weaker evidence than a real request."
        warn "  sudo apt install curl"
    else
        CODE=""
        for _ in $(seq 1 30); do
            CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                     "http://127.0.0.1:$PORT/" 2>/dev/null || true)"
            if [ -n "$CODE" ] && [ "$CODE" != "000" ]; then break; fi
            sleep 1
        done

        case "$CODE" in
            401)


                note "HTTP 401 on http://127.0.0.1:$PORT/ -- alive, and the token gate is armed"
                ;;
            200)
                warn "HTTP 200 with no credential at all: the token gate is OFF."
                warn "The ExecStart has lost --require-token (or --token), so"
                warn "anyone who reaches this port drives the workbench."
                warn "  systemctl cat $SERVICE | grep -A5 ExecStart"
                ;;
            000|"")
                die "nothing answered on http://127.0.0.1:$PORT/ after 30 s." \
                    "systemd says the unit is active, so it is either bound" \
                    "somewhere else or wedged before serve_forever()." \
                    "  journalctl -u $SERVICE -n 50 --no-pager" \
                    "  ss -ltnp | grep ':$PORT'"
                ;;
            *)
                die "unexpected HTTP $CODE from http://127.0.0.1:$PORT/." \
                    "  journalctl -u $SERVICE -n 50 --no-pager"
                ;;
        esac
    fi
fi


phase "8/8  Done"

note "repo at $(git -C "$REPO" rev-parse --short HEAD) on $BRANCH"
[ -n "$ARCHIVE" ] && note "state backup: $ARCHIVE"

if [ "$DRY_RUN" -eq 1 ]; then
    note "dry run -- nothing was changed."
else
    detail "The TLS proxy in front of this is a separate unit. If teammates"
    detail "report a certificate warning or a 502, look there, not here."
fi
