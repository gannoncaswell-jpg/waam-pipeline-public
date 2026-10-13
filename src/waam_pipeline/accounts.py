from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+!#$&*/=?^`{|}~-]{1,128}"
                      r"@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")


SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LEN = 10

MAX_PASSWORD_LEN = 256


FAIL_WINDOW_S = 15 * 60
MAX_FAILS_PER_ACCOUNT = 20
MAX_FAILS_PER_CLIENT = 60
MAX_TRACKED = 4096


MAX_ACCOUNTS = 200
MAX_REGISTRATIONS_PER_CLIENT = 5
REGISTER_WINDOW_S = 60 * 60

_LOCK = threading.Lock()
_FAILS: dict[str, list[float]] = {}
_REGISTRATIONS: dict[str, list[float]] = {}


class AccountError(Exception):
    pass


class FileLockTimeout(OSError):
    pass


@contextmanager
def exclusive_file_lock(path: Path, timeout: float = 3.0):

    handle = None
    descriptor = -1
    locked = False
    try:
        path = Path(path)
        if path.is_symlink():
            raise OSError("lock file must not be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        descriptor = -1
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("lock file is not a private regular file")
        if not info.st_size:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout("timed out waiting for the file lock") from None
                time.sleep(0.02)
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if handle is not None:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


@dataclass(frozen=True)
class Account:
    email: str
    salt: str
    hash: str
    n: int
    r: int
    p: int
    created: float
    last_seen: float = 0.0


    verified: bool = False

    def to_dict(self) -> dict:
        return {"email": self.email, "salt": self.salt, "hash": self.hash,
                "n": self.n, "r": self.r, "p": self.p,
                "created": self.created, "last_seen": self.last_seen,
                "verified": self.verified}


def credential_revision(account: Account) -> str:


    fields = account.to_dict()
    fields.pop("last_seen")
    return hashlib.sha256(json.dumps(fields, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _accounts_file() -> Path:


    return Path(__file__).resolve().parents[2] / ".waam-accounts.json"


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)


def password_problems(password: str) -> list[str]:


    if not isinstance(password, str):
        return ["Password must be text."]
    if len(password) < MIN_PASSWORD_LEN:
        return [f"Use at least {MIN_PASSWORD_LEN} characters for your password."]
    if len(password) > MAX_PASSWORD_LEN:
        return [f"Use at most {MAX_PASSWORD_LEN} characters for your password."]
    return []


def hash_password(password: str) -> tuple[str, str]:

    salt = secrets.token_bytes(16)
    return salt.hex(), _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P).hex()


def verify_password(account: Account | None, password: str) -> bool:


    if account is None:


        _derive(password, b"\x00" * 16, SCRYPT_N, SCRYPT_R, SCRYPT_P)
        return False
    try:
        want = bytes.fromhex(account.hash)
        salt = bytes.fromhex(account.salt)
    except ValueError:
        return False
    got = _derive(password, salt, account.n, account.r, account.p)
    return hmac.compare_digest(got, want)


def needs_rehash(account: Account) -> bool:


    return (account.n, account.r, account.p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def _prune_window(hits: dict[str, list[float]], now: float, window: float) -> None:
    for key in list(hits):
        kept = [t for t in hits[key] if now - t < window]
        if kept:
            hits[key] = kept
        else:
            del hits[key]
    if len(hits) > MAX_TRACKED:
        for key in sorted(hits, key=lambda k: hits[k][-1])[:len(hits) // 2]:
            del hits[key]


def _prune(now: float) -> None:
    _prune_window(_FAILS, now, FAIL_WINDOW_S)


def locked_out(email: str, client: str) -> str | None:

    now = time.time()
    with _LOCK:
        _prune(now)
        acct = len(_FAILS.get("a:" + email.lower(), ()))
        addr = len(_FAILS.get("c:" + client, ()))
    if acct >= MAX_FAILS_PER_ACCOUNT or addr >= MAX_FAILS_PER_CLIENT:
        return ("Too many failed attempts. Wait "
                f"{FAIL_WINDOW_S // 60} minutes and try again.")
    return None


def record_failure(email: str, client: str) -> None:
    now = time.time()
    with _LOCK:
        _prune(now)
        _FAILS.setdefault("a:" + email.lower(), []).append(now)
        _FAILS.setdefault("c:" + client, []).append(now)


def registration_allowed(client: str) -> str | None:

    now = time.time()
    with _LOCK:
        _prune_window(_REGISTRATIONS, now, REGISTER_WINDOW_S)
        used = len(_REGISTRATIONS.get(client, ()))
    if used >= MAX_REGISTRATIONS_PER_CLIENT:
        return ("Too many accounts have been created from this address. Wait "
                f"{REGISTER_WINDOW_S // 60} minutes and try again.")
    return None


def record_registration(client: str) -> None:

    now = time.time()
    with _LOCK:
        _prune_window(_REGISTRATIONS, now, REGISTER_WINDOW_S)
        _REGISTRATIONS.setdefault(client, []).append(now)


def clear_failures(email: str, client: str) -> None:
    with _LOCK:
        _FAILS.pop("a:" + email.lower(), None)
        _FAILS.pop("c:" + client, None)


def forget_rate_limits() -> None:

    with _LOCK:
        _FAILS.clear()
        _REGISTRATIONS.clear()


PURPOSE_VERIFY = "verify"
PURPOSE_RESET = "reset"

CODE_DIGITS = 6
CODE_TTL_S = 15 * 60
MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_S = 60
MAX_PENDING_CODES = 512

_CODES: dict[str, dict] = {}


def _prune_codes(now: float) -> None:
    for email in [e for e, c in _CODES.items() if c["expires"] < now]:
        del _CODES[email]
    if len(_CODES) > MAX_PENDING_CODES:
        for email in sorted(_CODES, key=lambda e: _CODES[e]["expires"])[:len(_CODES) // 2]:
            del _CODES[email]


def new_code(email: str, purpose: str = PURPOSE_VERIFY) -> str:


    if purpose not in (PURPOSE_VERIFY, PURPOSE_RESET):
        raise AccountError(f"unknown code purpose {purpose!r}")
    email = email.strip().lower()

    code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
    salt = secrets.token_bytes(16)
    now = time.time()
    with _LOCK:
        _prune_codes(now)
        _CODES[email] = {
            "salt": salt,


            "hash": _derive(code, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P),
            "expires": now + CODE_TTL_S,
            "tries": 0,
            "sent": now,
            "purpose": purpose,
        }
    return code


def code_pending(email: str, purpose: str | None = None) -> bool:
    with _LOCK:
        _prune_codes(time.time())
        entry = _CODES.get(email.strip().lower())
    return entry is not None and (purpose is None
                                  or entry.get("purpose") == purpose)


def resend_wait(email: str) -> int:


    with _LOCK:
        entry = _CODES.get(email.strip().lower())
        if not entry:
            return 0
        left = RESEND_COOLDOWN_S - (time.time() - entry["sent"])
    return max(0, int(left + 0.999))


def check_code(email: str, given: str, purpose: str = PURPOSE_VERIFY,
               *, before_consume: Callable[[], None] | None = None) -> None:


    email = email.strip().lower()
    given = "".join((given or "").split())
    now = time.time()
    with _LOCK:
        _prune_codes(now)
        entry = _CODES.get(email)
        if entry is None or entry.get("purpose") != purpose:
            raise AccountError(
                "The code has expired or is unavailable. Request a new code.")
        entry["tries"] += 1
        if entry["tries"] > MAX_CODE_ATTEMPTS:
            del _CODES[email]
            raise AccountError(
                "Too many incorrect codes. Request a new code.")
        salt, want = entry["salt"], entry["hash"]
    got = _derive(given, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    if not hmac.compare_digest(got, want):
        raise AccountError("The code is incorrect. Check the code in your email.")
    with _LOCK:
        if _CODES.get(email) is not entry:

            raise AccountError(
                "The code has expired or is unavailable. Request a new code.")
        if before_consume is not None:

            entry["tries"] -= 1
            before_consume()
        _CODES.pop(email, None)


def forget_codes() -> None:

    with _LOCK:
        _CODES.clear()


class Store:


    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else _accounts_file()
        self._lock = threading.Lock()

    @contextmanager
    def _writing(self):

        with self._lock:
            try:
                path = self.path.with_name(self.path.name + ".lock")
                with exclusive_file_lock(path):
                    yield
            except FileLockTimeout:
                raise AccountError(
                    "Account information is busy. Please try again shortly.") from None
            except OSError:
                raise AccountError("Account information could not be updated. Please try again shortly.") from None

    def _read(self) -> dict[str, Account]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):


            raise AccountError(
                "The account store is unreadable. Contact the administrator to restore sign-in.")
        if not isinstance(raw, dict) or not isinstance(raw.get("accounts", []), list):
            raise AccountError("The account store is unreadable. Contact the administrator to restore sign-in.")
        out: dict[str, Account] = {}
        for rec in raw.get("accounts") or ():
            try:
                email = str(rec["email"]).strip().lower()
                out[email] = Account(
                    email=email, salt=str(rec["salt"]), hash=str(rec["hash"]),
                    n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]),
                    created=float(rec.get("created", 0.0)),
                    last_seen=float(rec.get("last_seen", 0.0)),
                    verified=bool(rec.get("verified", False)))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _write(self, accounts: dict[str, Account]) -> None:


        body = json.dumps(
            {"_what": "waam-pipeline local accounts. scrypt hashes, no "
                      "passwords. Machine-written: do not hand-edit. The "
                      "roster and blocklist live in .waam-auth.json.",
             "accounts": [a.to_dict() for a in accounts.values()]},
            indent=2) + "\n"
        tmp: Path | None = None
        try:


            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=self.path.name + ".", suffix=".tmp", delete=False) as out:
                tmp = Path(out.name)
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)
            tmp = None
            if os.name != "nt":
                directory_fd = os.open(
                    self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def get(self, email: str) -> Account | None:
        with self._lock:
            return self._read().get(email.strip().lower())

    def emails(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    def register(self, email: str, password: str, *, limit: int | None = None) -> Account:

        email = (email or "").strip().lower()
        if not EMAIL_RE.fullmatch(email):
            raise AccountError("Enter a valid email address.")
        problems = password_problems(password)
        if problems:
            raise AccountError(problems[0])
        cap = MAX_ACCOUNTS if limit is None else int(limit)
        salt, digest = hash_password(password)
        with self._writing():
            accounts = self._read()
            if email in accounts:


                raise AccountError(
                    "There is already an account for that address. Sign in or reset your password.")


            if len(accounts) >= cap:
                raise AccountError(
                    "This workbench is not accepting new accounts. "
                    "Contact the administrator.")
            acct = Account(email=email, salt=salt, hash=digest, n=SCRYPT_N,
                           r=SCRYPT_R, p=SCRYPT_P, created=time.time())
            accounts[email] = acct
            self._write(accounts)
        return acct

    def authenticate(self, email: str, password: str) -> Account:

        email = (email or "").strip().lower()
        with self._lock:
            accounts = self._read()
            acct = accounts.get(email)
        if not verify_password(acct, password or ""):
            raise AccountError("The email address and password do not match.")
        assert acct is not None
        with self._writing():
            accounts = self._read()
            current = accounts.get(email)
            if current is None or not hmac.compare_digest(
                    credential_revision(current), credential_revision(acct)):


                raise AccountError("The email address and password do not match.")
            if current is not None:
                fresh = current
                if needs_rehash(current):
                    salt, digest = hash_password(password)
                    fresh = Account(email=email, salt=salt, hash=digest,
                                    n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                                    created=current.created,
                                    last_seen=time.time(), verified=current.verified)
                else:
                    fresh = Account(**{**current.to_dict(),
                                       "last_seen": time.time()})
                accounts[email] = fresh
                self._write(accounts)
                return fresh
        return acct

    def mark_verified(self, email: str) -> Account:

        email = (email or "").strip().lower()
        with self._writing():
            accounts = self._read()
            current = accounts.get(email)
            if current is None:
                raise AccountError(f"no account for {email}")
            if current.verified:
                return current
            acct = Account(**{**current.to_dict(), "verified": True})
            accounts[email] = acct
            self._write(accounts)
        return acct

    def reset_password(self, email: str, password: str,
                       *, verified: bool | None = None) -> Account:


        email = (email or "").strip().lower()
        problems = password_problems(password)
        if problems:
            raise AccountError(problems[0])
        salt, digest = hash_password(password)
        with self._writing():
            accounts = self._read()
            current = accounts.get(email)
            if current is None:
                raise AccountError(f"no account for {email}")
            acct = Account(email=email, salt=salt, hash=digest, n=SCRYPT_N,
                           r=SCRYPT_R, p=SCRYPT_P, created=current.created,
                           last_seen=current.last_seen,
                           verified=(current.verified if verified is None
                                     else bool(verified)))
            accounts[email] = acct
            self._write(accounts)
        return acct

    def delete(self, email: str) -> bool:


        email = (email or "").strip().lower()
        with self._writing():
            accounts = self._read()
            if email not in accounts:
                return False
            del accounts[email]
            self._write(accounts)
        return True


def _main(argv=None) -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(
        prog="python -m waam_pipeline.accounts",
        description="List, reset and remove workbench accounts.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="every account, and when it was last used")
    r = sub.add_parser("reset", help="set a new password for somebody")
    r.add_argument("email")
    r.add_argument("--password", default=None,
                   help="skip the prompt (it will be visible in your shell "
                        "history - prefer the prompt)")
    d = sub.add_parser("delete", help="remove an account record")
    d.add_argument("email")
    args = ap.parse_args(argv)

    store = Store()

    if args.cmd == "list" or args.cmd is None:
        emails = store.emails()
        if not emails:
            print(f"no accounts yet ({store.path.name} does not exist)")
        else:
            print(f"{len(emails)} account(s) in {store.path.name}:")
        for email in emails:
            acct = store.get(email)
            seen = (time.strftime("%Y-%m-%d %H:%M", time.localtime(acct.last_seen))
                    if acct.last_seen else "never signed in")
            stale = "  (weak hash - upgrades on next sign-in)" if needs_rehash(acct) else ""
            flag = "" if acct.verified else "  (email NOT verified)"
            print(f"  {email:34} last seen {seen}{stale}{flag}")


        try:
            import json as _json

            from . import oidc as _oidc

            cfg = _json.loads(_oidc._auth_config_file().read_text(encoding="utf-8"))
            expected = [str(e).strip().lower() for e in (cfg.get("users") or {})]
        except Exception:
            expected = []
        if expected:
            missing = [e for e in expected if e not in set(emails)]
            print()
            if missing:
                print(f"  still to sign up ({len(missing)}): " + ", ".join(missing))
                print("  Keep the shared onboarding link open until they have.")
            else:
                print("  everyone named in .waam-auth.json has an account.")
                print("  The shared onboarding link can be retired now:")
                print(f"      delete {_oidc._auth_config_file().parent / '.waam-token'}")
                print("  It stops working on the very next request - no restart -")
                print("  and signing up never needed it in the first place.")

        print("\nTo remove someone's ACCESS, add their address to `blocked` in")
        print(".waam-auth.json - that also ends the session they are holding.")
        print("Deleting the account here does not stop them registering again.")
        return 0

    if args.cmd == "reset":
        password = args.password or getpass.getpass("new password: ")
        problems = password_problems(password)
        if problems:
            print(problems[0])
            return 1
        try:
            store.reset_password(args.email, password)
        except AccountError as exc:
            print(str(exc))
            return 1
        print(f"password set for {args.email.strip().lower()}. Tell them "
              "directly - not by email, and not in a group chat.")
        return 0

    if args.cmd == "delete":
        if store.delete(args.email):
            print(f"removed the account record for {args.email.strip().lower()}.")
            print("NOTE: with open registration they can create it again. To")
            print("actually revoke access, add the address to `blocked` in")
            print(".waam-auth.json instead.")
            return 0
        print(f"no account for {args.email.strip().lower()}")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
