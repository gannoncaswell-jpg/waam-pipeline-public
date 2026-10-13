from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from contextlib import contextmanager
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading

from .accounts import exclusive_file_lock
from .job_archive import safe_read

FILE_NAME = ".waam-run-log-excel.json"
MAX_HEADER_BYTES = 2048
MAX_STORE_BYTES = 2 * 1024 * 1024
MAX_CREDENTIALS = 4096
_LOCK = threading.Lock()
_HEX = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_EMPTY_DIGEST = "0" * 64


class CredentialError(OSError):
    pass


def _email(value: str) -> str:
    if not isinstance(value, str) or len(value) > 320:
        raise ValueError("A valid account email is required.")
    value = value.strip().casefold()
    if (not re.fullmatch(r"[^@\s:]+@[^@\s:]+\.[^@\s:]+", value)
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or len(value.encode("utf-16-le")) > 640):
        raise ValueError("A valid account email is required.")
    return value


def _scope(value: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError("A current account authorization scope is required.")
    return value


def _path(path: Path) -> Path:
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("linked credential storage")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("credential storage is not a regular unlinked file")
        if os.name == "posix" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("credential storage permissions are not private")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential metadata")
        result[key] = value
    return result


def _load(path: Path) -> dict:
    try:
        body = safe_read(_path(path), MAX_STORE_BYTES)
    except FileNotFoundError:
        return {}
    data = json.loads(body, object_pairs_hook=_unique_object)
    if (not isinstance(data, dict) or set(data) != {"version", "credentials"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["credentials"], dict)
            or len(data["credentials"]) > MAX_CREDENTIALS):
        raise ValueError("invalid credential metadata")
    records = data["credentials"]
    for email, record in records.items():
        if (_email(email) != email or not isinstance(record, dict)
                or set(record) != {"digest", "scope", "created_at"}
                or not isinstance(record["digest"], str) or not _HEX.fullmatch(record["digest"])
                or not isinstance(record["created_at"], str)):
            raise ValueError("invalid credential record")
        _scope(record["scope"])
        created = dt.datetime.fromisoformat(record["created_at"])
        if created.tzinfo is None:
            raise ValueError("invalid credential timestamp")
    return records


def _write(path: Path, records: dict) -> None:
    path = _path(path)
    body = json.dumps({"version": 1, "credentials": records}, sort_keys=True,
                      indent=2, allow_nan=False).encode("utf-8") + b"\n"
    if len(body) > MAX_STORE_BYTES:
        raise ValueError("credential storage capacity reached")
    descriptor, filename = tempfile.mkstemp(prefix=".waam-excel-pending-", dir=path.parent)
    pending = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            os.chmod(pending, 0o600)
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        _path(path)
        os.replace(pending, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        pending.unlink(missing_ok=True)


@contextmanager
def _mutation(path: Path):
    with _LOCK:
        path = _path(path)
        lock_path = path.with_name(path.name + ".lock")
        _path(lock_path)
        with exclusive_file_lock(lock_path):
            yield path


def status(path: Path, email: str, *, scope: str) -> dict:

    email, scope = _email(email), _scope(scope)
    try:
        with _LOCK:
            record = _load(path).get(email)
        enabled = bool(record and hmac.compare_digest(record["scope"], scope))
        return {"enabled": enabled, "created_at": record["created_at"] if enabled else None}
    except (OSError, ValueError, TypeError):
        raise CredentialError("Excel credential storage is unavailable.") from None


def create(path: Path, email: str, *, scope: str) -> dict:

    email, scope = _email(email), _scope(scope)
    try:
        with _mutation(path) as path:
            records = _load(path)
            if email not in records and len(records) >= MAX_CREDENTIALS:
                raise ValueError("credential capacity reached")
            password = secrets.token_urlsafe(32)
            created = dt.datetime.now(dt.timezone.utc).isoformat()
            records[email] = {"digest": hashlib.sha256(password.encode("ascii")).hexdigest(),
                              "scope": scope, "created_at": created}
            _write(path, records)
        return {"enabled": True, "created_at": created, "password": password}
    except (OSError, ValueError, TypeError):
        raise CredentialError("Excel credential could not be created. Check its status before retrying.") from None


def revoke(path: Path, email: str) -> dict:

    email = _email(email)
    try:
        with _mutation(path) as path:
            records = _load(path)
            if email in records:
                del records[email]
                _write(path, records)
        return {"enabled": False, "created_at": None}
    except (OSError, ValueError, TypeError):
        raise CredentialError("Excel credential could not be revoked. Check its status before retrying.") from None


def authenticate(path: Path, authorization: str | None, *,
                 scope_for_email: Callable[[str], str | None]) -> str | None:

    if (not isinstance(authorization, str) or len(authorization) > MAX_HEADER_BYTES
            or not authorization.isascii()):
        return None
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.casefold() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, separator, password = decoded.partition(":")
        if not separator or not _TOKEN.fullmatch(password):
            return None
        email = _email(username)
    except (binascii.Error, UnicodeError, ValueError):
        return None
    digest = hashlib.sha256(password.encode("ascii")).hexdigest()
    try:
        with _LOCK:
            record = _load(path).get(email)
        expected = record["digest"] if record else _EMPTY_DIGEST
        if not hmac.compare_digest(digest, expected) or record is None:
            return None
        current_scope = scope_for_email(email)
        if current_scope is None or not hmac.compare_digest(record["scope"], _scope(current_scope)):
            return None
        return email
    except (OSError, ValueError, TypeError):
        raise CredentialError("Excel credential verification is unavailable.") from None
