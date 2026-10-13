from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import oidc


def _config_path(path: Path | None = None) -> Path:


    return path or oidc._auth_config_file()


def _read_config(path: Path) -> dict:


    return json.loads(path.read_text(encoding="utf-8"))


def _write_config(path: Path, raw: dict) -> None:


    tmp = path.with_suffix(path.suffix + ".tmp")


    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(raw, indent=2) + "\n")
    try:


        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _normalise(email: str) -> str:
    return (email or "").strip().lower()


def pending_requests(log_path: Path | None = None) -> list[str]:


    from . import server
    path = log_path or server.ACCESS_LOG
    if not path.exists():
        return []
    seen: list[str] = []
    have: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("outcome") != "pending":
            continue
        email = _normalise(row.get("email") or "")
        if email and email not in have:
            have.add(email)
            seen.append(email)
    return seen


def _approved_and_roster(raw: dict) -> tuple[set[str], set[str]]:


    approved = {_normalise(a) for a in (raw.get("approved") or []) if _normalise(a)}
    roster = {_normalise(e) for e in (raw.get("users") or {}) if _normalise(e)}
    return approved, roster


def _read_for_edit(path: Path) -> tuple[dict | None, str]:


    if not path.exists():
        return None, f"{path.name} does not exist"
    try:
        raw = _read_config(path)
    except (OSError, ValueError) as exc:
        return None, f"{path.name} is unreadable ({exc.__class__.__name__})"
    if not isinstance(raw, dict):
        return None, f"{path.name} is not a JSON object"
    return raw, ""


def _config_list(raw: dict, key: str) -> list:


    value = raw.get(key)
    return value if isinstance(value, list) else []


def approve_email(email: str, path: Path | None = None) -> tuple[bool, str]:


    path = _config_path(path)
    email = _normalise(email)
    if not oidc._EMAIL_RE.fullmatch(email):
        return False, "not an email address"
    raw, err = _read_for_edit(path)
    if raw is None:
        return False, err
    approved = _config_list(raw, "approved")
    have, roster = _approved_and_roster(raw)
    if email in roster:


        return False, "on the roster already (admitted without approval)"
    if email in have:
        return False, "already approved"
    approved.append(email)
    raw["approved"] = approved
    try:
        _write_config(path, raw)
    except OSError as exc:
        return False, f"could not write {path.name} ({exc.__class__.__name__})"
    return True, "approved"


def block_email(email: str, path: Path | None = None) -> tuple[bool, str]:


    path = _config_path(path)
    email = _normalise(email)
    if not oidc._EMAIL_RE.fullmatch(email):
        return False, "not an email address"
    raw, err = _read_for_edit(path)
    if raw is None:
        return False, err


    admins = {_normalise(a) for a in (raw.get("admins") or [])
              if isinstance(a, str)}
    if email in admins:
        return False, ("that address is an administrator; remove it from "
                       "`admins` first if you really mean to revoke it")
    blocked = _config_list(raw, "blocked")
    have = {_normalise(b) for b in blocked if _normalise(b)}
    if email in have:
        return False, "already blocked"
    blocked.append(email)
    raw["blocked"] = blocked
    try:
        _write_config(path, raw)
    except OSError as exc:
        return False, f"could not write {path.name} ({exc.__class__.__name__})"
    return True, "blocked"


def _do_approve(emails: list[str], path: Path) -> int:
    if not path.exists():
        print(f"{path.name} does not exist, so there is no sign-in config to "
              "approve into. See docs/sso-setup.md.")
        return 1
    try:
        raw = _read_config(path)
    except (OSError, ValueError) as exc:

        print(f"{path.name} is unreadable ({exc.__class__.__name__}); not "
              "touching it.")
        return 1
    if not isinstance(raw, dict):
        print(f"{path.name} is not a JSON object; not touching it.")
        return 1

    approved = raw.get("approved")
    if not isinstance(approved, list):


        approved = []
    have, roster = _approved_and_roster(raw)

    changed = 0
    for given in emails:
        email = _normalise(given)
        if not oidc._EMAIL_RE.fullmatch(email):
            print(f"  {given}: not an email address, skipped")
            continue
        if email in have:
            print(f"  {email}: already approved")
            continue
        if email in roster:


            print(f"  {email}: on the roster already, so it is admitted without "
                  "approval")
            continue
        approved.append(email)
        have.add(email)
        changed += 1
        print(f"  {email}: approved")

    if not changed:
        print("nothing changed.")
        return 0

    raw["approved"] = approved
    try:
        _write_config(path, raw)
    except OSError as exc:
        print(f"could not write {path.name} ({exc.__class__.__name__}); "
              "nothing changed.")
        return 1
    print(f"\nwrote {path.name}: {changed} newly approved. It takes effect on "
          "their next request (the server re-reads the file; no restart).")
    if not raw.get("approval_required"):
        print(f"note: approval_required is not on in {path.name}, so the gate "
              "is not enforced yet and everyone gets in regardless. Set it true "
              "to hold new sign-ups for approval.")
    return 0


def _do_list(path: Path) -> int:
    waiting = pending_requests()
    approved: set[str] = set()
    roster: set[str] = set()
    if path.exists():
        try:
            raw = _read_config(path)
            if isinstance(raw, dict):
                approved, roster = _approved_and_roster(raw)
        except (OSError, ValueError):


            pass

    still = [e for e in waiting if e not in approved and e not in roster]
    from . import server
    if not still:
        if waiting:
            print("no one is waiting: every 'pending' address in the log is "
                  "already approved or on the roster.")
        else:
            print(f"no one is waiting: no 'pending' entries in {server.ACCESS_LOG.name}.")
        return 0
    print(f"{len(still)} waiting for approval:")
    for email in still:
        print(f"  {email}")
    print("\nApprove with:  python -m waam_pipeline.approve " + " ".join(still))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m waam_pipeline.approve",
        description="Approve people held pending by the sign-in approval gate, "
                    "or list who is waiting.")
    ap.add_argument("emails", nargs="*", metavar="EMAIL",
                    help="address(es) to add to `approved` in .waam-auth.json")
    ap.add_argument("--list", action="store_true", dest="list_pending",
                    help="print addresses logged 'pending' that are not yet "
                         "approved, and exit")
    ap.add_argument("--file", type=Path, default=None,
                    help="use this config file instead of .waam-auth.json")
    args = ap.parse_args(argv)

    path = _config_path(args.file)
    if args.list_pending:
        if args.emails:
            ap.error("--list takes no addresses")
        return _do_list(path)
    if not args.emails:
        ap.error("give one or more addresses to approve, or --list")
    return _do_approve(args.emails, path)


if __name__ == "__main__":
    sys.exit(main())
