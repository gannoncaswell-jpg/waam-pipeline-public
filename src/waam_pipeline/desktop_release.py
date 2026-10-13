'Publish and inspect the desktop builds served by the workbench, one per platform.'
from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
from dataclasses import dataclass
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
from typing import BinaryIO

from . import accounts


@dataclass(frozen=True)
class Platform:
    key: str
    manifest: str
    label: str
    signed: str
    suffixes: frozenset[str]
    update_suffixes: frozenset[str]


PLATFORMS = {
    "macos-arm64": Platform("macos-arm64", "desktop.json", "macOS, Apple Silicon (arm64)",
                            "ad-hoc (development)", frozenset({".dmg", ".zip"}),
                            frozenset({".zip"})),
    "windows-x64": Platform("windows-x64", "desktop-windows-x64.json", "Windows 10/11 (x64)",
                            "unsigned (development)", frozenset({".exe"}),
                            frozenset({".exe"})),
}
DEFAULT_PLATFORM = "macos-arm64"
MANIFEST_NAME = PLATFORMS[DEFAULT_PLATFORM].manifest
PLATFORM = PLATFORMS[DEFAULT_PLATFORM].label
SIGNED = PLATFORMS[DEFAULT_PLATFORM].signed
MAX_MANIFEST_BYTES = 64 * 1024
MAX_VERSION_BYTES = 200
MAX_NOTES_BYTES = 32 * 1024
MAX_FILE_NAME_BYTES = 240
COPY_CHUNK_BYTES = 1024 * 1024
LOCK_NAME = ".desktop-release.lock"
LOCK_TIMEOUT_S = 120.0


RELEASE_MESSAGE_HEADER = "waam-studio-release-v1"
SIGNATURE_BYTES = 64
PUBLIC_KEY_BYTES = 32
MAX_KEY_FILE_BYTES = 4096
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")

_MAX_JS_INTEGER = 2**53 - 1


PUBLIC_KEY_FILE_ENV = "WAAM_DESKTOP_RELEASE_PUBLIC_KEY_FILE"
DEFAULT_PUBLIC_KEY_FILE = "/etc/waam/desktop-release-public-key"

_CHECK_KEY_FILE_MODE = os.name == "posix"

_LOG = logging.getLogger(__name__)
_HASH_CACHE: dict[tuple[int, int, int, int, int], str] = {}
_HASH_LOCK = threading.Lock()


@dataclass
class OpenRelease:
    manifest: dict
    source: BinaryIO

    def close(self) -> None:
        self.source.close()

    def __enter__(self) -> OpenRelease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def downloads_dir(directory: Path | str | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    return Path(os.environ.get("WAAM_DOWNLOADS_DIR") or "/srv/waam/downloads")


def _text(value: object, field: str, maximum: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{field} must be {qualifier}")
    length = len(value.encode("utf-8"))
    if length > maximum:
        raise ValueError(f"{field} is too long (maximum {maximum} UTF-8 bytes)")
    return value


def _platform(key: str) -> Platform:
    try:
        return PLATFORMS[key]
    except KeyError:
        raise ValueError(f"unknown desktop platform {key!r}") from None


def platform_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    for platform in PLATFORMS.values():
        if suffix in platform.suffixes:
            return platform.key
    allowed = ", ".join(sorted(s for item in PLATFORMS.values() for s in item.suffixes))
    raise ValueError(f"file must end in one of {allowed}")


def _file_name(value: object, platform: str | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("file must be a non-empty name")
    if "/" in value or "\\" in value or Path(value).name != value:
        raise ValueError("file must not contain path separators")
    if platform is None:
        platform_for(value)
    elif Path(value).suffix.lower() not in _platform(platform).suffixes:
        suffixes = " or ".join(sorted(_platform(platform).suffixes))
        raise ValueError(f"{platform} file must end in {suffixes}")
    if not value.isascii() or '"' in value or any(ord(char) < 32 for char in value):
        raise ValueError("file must be a safe ASCII download name")
    if len(value.encode("ascii")) > MAX_FILE_NAME_BYTES:
        raise ValueError(f"file name is too long (maximum {MAX_FILE_NAME_BYTES} bytes)")
    return value


def release_version(value: object, field: str = "--version") -> str:

    text = _text(value, field, MAX_VERSION_BYTES, allow_empty=False)
    match = _VERSION.fullmatch(text)
    if match is None or any(int(part) > _MAX_JS_INTEGER for part in match.groups()):
        raise ValueError(f"{field} must be MAJOR.MINOR.PATCH, e.g. 0.2.0 "
                         f"(no 'v', pre-release tag or leading zeros), not {text!r}")
    return text


def _sha256_text(value: object) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return value


def _signed_size(size: object) -> int:
    if type(size) is not int or size <= 0:
        raise ValueError("size must be a positive integer")
    if size > _MAX_JS_INTEGER:
        raise ValueError(f"a signed release must be at most {_MAX_JS_INTEGER} bytes "
                         "(2^53 - 1, the largest size the updater compares exactly)")
    return size


def release_message(platform: str, version: str, size: int, sha256: str) -> bytes:

    key = _platform(platform).key
    version = release_version(version, "version")
    size = _signed_size(size)
    digest = _sha256_text(sha256)
    return (f"{RELEASE_MESSAGE_HEADER}\nplatform={key}\nversion={version}\n"
            f"size={size}\nsha256={digest}\n").encode("utf-8")


def _strict_base64(text: object, field: str, length: int) -> bytes:
    invalid = f"{field} must be standard padded base64"
    if not isinstance(text, str):
        raise ValueError(invalid)
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raise ValueError(invalid) from None

    if base64.b64encode(raw).decode("ascii") != text:
        raise ValueError(invalid)
    if len(raw) != length:
        raise ValueError(f"{field} decodes to {len(raw)} bytes; expected exactly {length}")
    return raw


def _signature_text(value: object, field: str = "signature") -> str:
    return base64.b64encode(_strict_base64(value, field, SIGNATURE_BYTES)).decode("ascii")


def _base64_file_body(body: bytes, field: str, length: int) -> bytes:
    if len(body) > MAX_KEY_FILE_BYTES:
        raise ValueError(f"{field} is too large")
    try:

        text = body.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise ValueError(f"{field} must hold base64 text") from None
    return _strict_base64(text, field, length)


def _base64_argument(value: str, field: str, length: int) -> bytes:

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    path = Path(value)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        try:
            return _strict_base64(value.strip(), field, length)
        except ValueError as exc:
            raise ValueError(f"{exc}, or name a readable file") from None
    with path.open("rb") as source:
        body = source.read(MAX_KEY_FILE_BYTES + 1)
    return _base64_file_body(body, f"{field} file {path}", length)


def read_signature(value: str) -> str:

    return base64.b64encode(_base64_argument(value, "--signature", SIGNATURE_BYTES)).decode("ascii")


def read_public_key(value: str) -> bytes:

    return _base64_argument(value, "--public-key", PUBLIC_KEY_BYTES)


def pinned_public_key_file() -> tuple[Path, bool]:

    configured = os.environ.get(PUBLIC_KEY_FILE_ENV)
    return Path(configured or DEFAULT_PUBLIC_KEY_FILE), bool(configured)


def read_pinned_public_key(path: Path, *, required: bool) -> bytes | None:

    field = f"pinned release public key {path}"
    try:
        source = path.open("rb")
    except FileNotFoundError:
        if required:
            raise ValueError(f"{PUBLIC_KEY_FILE_ENV} names {path}, which does not exist") from None
        return None
    except OSError as exc:

        raise ValueError(f"{field} cannot be read: {exc}") from None
    with source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"{field} is not a regular file")
        if _CHECK_KEY_FILE_MODE and mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"{field} must not be group- or world-writable "
                             "(install it root-owned, mode 0644)")
        body = source.read(MAX_KEY_FILE_BYTES + 1)
    return _base64_file_body(body, field, PUBLIC_KEY_BYTES)


def _verification_key(public_key: bytes | None) -> tuple[bytes, str]:
    path, configured = pinned_public_key_file()
    pinned = read_pinned_public_key(path, required=configured)
    if public_key is not None:
        if pinned is not None and public_key != pinned:
            raise ValueError(f"--public-key differs from the pinned release public key {path}; "
                             "both must be the key built into the desktop app")
        return public_key, "--public-key"
    if pinned is not None:
        return pinned, f"the pinned release public key {path}"
    raise ValueError("a release signature must verify before it is published: pass --public-key, "
                     f"or pin the release public key in {path} (or name it with "
                     f"{PUBLIC_KEY_FILE_ENV})")


def _ed25519_verifier(public_key: bytes, source: str = "--public-key"):

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ValueError("release signature verification needs the 'cryptography' package "
                         "(the 'release' extra)") from exc
    if not isinstance(public_key, bytes) or len(public_key) != PUBLIC_KEY_BYTES:
        raise ValueError(f"{source} must be exactly {PUBLIC_KEY_BYTES} bytes")
    try:
        key = Ed25519PublicKey.from_public_bytes(public_key)
    except ValueError as exc:
        raise ValueError(f"{source} is not an Ed25519 public key: {exc}") from None

    def verify(signature: str, message: bytes) -> None:
        try:
            key.verify(base64.b64decode(signature), message)
        except InvalidSignature:
            raise ValueError(f"release signature does not verify against {source} "
                             "for this file, platform and version") from None
    return verify


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("published_at must be a timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError("published_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("published_at must include a timezone")
    return value


def _manifest(value: object, platform: str = DEFAULT_PLATFORM) -> dict:
    target = _platform(platform)
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    required = {
        "version", "file", "size", "sha256", "platform", "signed",
        "notarized", "published_at", "notes",
    }
    allowed = required | {"mtime_ns", "signature"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        missing = sorted(required - set(value))
        extra = sorted(set(value) - allowed)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise ValueError("manifest fields are invalid (" + "; ".join(detail) + ")")
    version = _text(value["version"], "version", MAX_VERSION_BYTES, allow_empty=False)
    name = _file_name(value["file"], platform)
    if type(value["size"]) is not int or value["size"] <= 0:
        raise ValueError("size must be a positive integer")
    mtime_ns = value.get("mtime_ns")
    if mtime_ns is not None and (type(mtime_ns) is not int or mtime_ns <= 0):
        raise ValueError("mtime_ns must be a positive integer")
    digest = _sha256_text(value["sha256"])
    if mtime_ns is not None and not name.startswith(digest[:16] + "-"):
        raise ValueError("file must use the sha256-prefixed immutable name")
    if value["platform"] != target.label:
        raise ValueError(f"platform must be {target.label!r}")
    if value["signed"] != target.signed:
        raise ValueError(f"signed must be {target.signed!r}")
    if value["notarized"] is not False:
        raise ValueError("notarized must be false for this development build")
    published_at = _timestamp(value["published_at"])
    notes = _text(value["notes"], "notes", MAX_NOTES_BYTES, allow_empty=True)
    checked = {
        "version": version, "file": name, "size": value["size"],
        "sha256": digest, "platform": target.label, "signed": target.signed,
        "notarized": False, "published_at": published_at, "notes": notes,
    }
    if mtime_ns is not None:
        checked["mtime_ns"] = mtime_ns
    if "signature" in value:

        checked["signature"] = _signature_text(value["signature"])
        release_version(version, "version")
        _signed_size(value["size"])
        if Path(name).suffix.lower() not in target.update_suffixes:
            raise ValueError(f"a signed {target.key} release must be a "
                             + " or ".join(sorted(target.update_suffixes)))
    return checked


def _serialize_manifest(release: dict, platform: str) -> bytes:
    checked = _manifest(release, platform)
    body = (json.dumps(checked, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest is too large (maximum {MAX_MANIFEST_BYTES} bytes)")
    return body


def _read_json(path: Path) -> object:
    with path.open("rb") as source:
        body = source.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest is too large (maximum {MAX_MANIFEST_BYTES} bytes)")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid UTF-8 JSON") from exc


def _descriptor_key(info: os.stat_result) -> tuple[int, int, int, int, int]:
    mtime = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
    ctime = getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))
    return info.st_dev, info.st_ino, info.st_size, mtime, ctime


def _hash_source(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    source.seek(0)
    for block in iter(lambda: source.read(COPY_CHUNK_BYTES), b""):
        digest.update(block)
    source.seek(0)
    return digest.hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"release asset is not a file: {path}")
            digest = _hash_source(source)
            after = os.fstat(source.fileno())
    except OSError as exc:
        raise ValueError(f"release asset cannot be read: {path}: {exc}") from exc
    if _descriptor_key(before) != _descriptor_key(after):
        raise ValueError("release asset changed while it was being hashed")
    return before.st_size, digest


def _invalid(root: Path, reason: str, *, absent: bool = False) -> None:
    message = "desktop release unavailable in %s: %s"
    (_LOG.info if absent else _LOG.warning)(message, root, reason)


def _open_regular_entry(root: Path, name: str) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    if nofollow and os.open in getattr(os, "supports_dir_fd", ()):
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                descriptor = os.open(name, flags | nofollow, dir_fd=directory)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError("release file must not be a symbolic link") from exc
                raise
        finally:
            os.close(directory)
    else:
        path = root / name
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("release file must not be a symbolic link")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("release file is not a regular file")
        descriptor = os.open(path, flags)
        try:
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("release file changed while it was being opened")
        except BaseException:
            os.close(descriptor)
            descriptor = -1
            raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("release file is not a regular file")
        source = os.fdopen(descriptor, "rb")
        descriptor = -1
        return source
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def open_release(directory: Path | str | None = None,
                 platform: str = DEFAULT_PLATFORM) -> OpenRelease | None:
    root = downloads_dir(directory)
    manifest_name = _platform(platform).manifest
    manifest_path = root / manifest_name
    for attempt in range(2):
        try:
            release = _manifest(_read_json(manifest_path), platform)
        except FileNotFoundError:
            _invalid(root, f"{manifest_name} is absent", absent=True)
            return None
        except (OSError, ValueError) as exc:
            _invalid(root, str(exc))
            return None

        try:
            source = _open_regular_entry(root, release["file"])
        except FileNotFoundError:
            if attempt == 0:
                continue
            _invalid(root, "release file is absent")
            return None
        except (OSError, ValueError) as exc:
            _invalid(root, f"release file cannot be opened: {exc}")
            return None
        try:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("release file is not a regular file")
            if info.st_size != release["size"]:
                raise ValueError(
                    f"release size is {info.st_size}, manifest says {release['size']}")
            mtime = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
            if "mtime_ns" in release and mtime != release["mtime_ns"]:
                raise ValueError(
                    f"release mtime is {mtime}, manifest says {release['mtime_ns']}")
            key = _descriptor_key(info)
            with _HASH_LOCK:
                cached = key in _HASH_CACHE
                if cached:
                    actual = _HASH_CACHE[key]
                else:
                    actual = _hash_source(source)
                    if _descriptor_key(os.fstat(source.fileno())) != key:
                        raise ValueError("release file changed while it was being verified")
                    _HASH_CACHE[key] = actual
                if cached and actual != release["sha256"]:
                    actual = _hash_source(source)
                    if _descriptor_key(os.fstat(source.fileno())) != key:
                        raise ValueError("release file changed while it was being verified")
                    _HASH_CACHE[key] = actual
            if actual != release["sha256"]:
                raise ValueError("release sha256 does not match the manifest")
            source.seek(0)
            return OpenRelease(release, source)
        except (OSError, ValueError) as exc:
            source.close()
            _invalid(root, str(exc))
            return None
    return None


def load(directory: Path | str | None = None, platform: str = DEFAULT_PLATFORM) -> dict | None:
    opened = open_release(directory, platform)
    if opened is None:
        return None
    with opened:
        return dict(opened.manifest)


def load_all(directory: Path | str | None = None) -> dict[str, dict]:

    releases = {}
    for key in PLATFORMS:
        release = load(directory, key)
        if release is not None:
            releases[key] = release
    return releases


def _expected_digest(value: str | None) -> str | None:
    if value is None:
        return None
    digest = value.strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("--sha256 must be exactly 64 hexadecimal characters")
    return digest


def _committed_name(root: Path, platform: str) -> str | None:
    try:
        value = _manifest(_read_json(root / _platform(platform).manifest), platform)
    except FileNotFoundError:
        return None
    return value["file"]


def _previous_name(root: Path, platform: str) -> str | None:
    try:
        return _committed_name(root, platform)
    except (OSError, ValueError):
        return None


def _fsync_directory(root: Path) -> None:
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        return
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_if_uncommitted(root: Path, name: str) -> None:

    for platform in PLATFORMS:
        try:
            committed = _committed_name(root, platform)
        except (OSError, ValueError) as exc:
            _LOG.warning("desktop release cleanup kept %s: manifest cannot be verified: %s", name, exc)
            return
        if committed is None:
            continue
        if os.path.normcase(committed) == os.path.normcase(name):
            return
        try:
            if os.path.samefile(root / committed, root / name):
                return
        except OSError:
            pass
    try:
        (root / name).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _LOG.warning("old desktop release %s could not be removed: %s", name, exc)


def _copy_and_hash(source: Path, root: Path, mtime_ns: int) -> tuple[Path, int, str]:
    descriptor, temporary = tempfile.mkstemp(prefix=".desktop-release-", suffix=".tmp", dir=root)
    pending = Path(temporary)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            for block in iter(lambda: incoming.read(COPY_CHUNK_BYTES), b""):
                output.write(block)
                digest.update(block)
                size += len(block)
            output.flush()
            os.fsync(output.fileno())
        os.utime(pending, ns=(mtime_ns, mtime_ns))
        with pending.open("r+b") as copied:
            os.fsync(copied.fileno())
        actual_mtime = getattr(pending.stat(), "st_mtime_ns", None)
        if actual_mtime != mtime_ns:
            raise OSError(f"filesystem stored release mtime as {actual_mtime}, expected {mtime_ns}")
        return pending, size, digest.hexdigest()
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        pending.unlink(missing_ok=True)
        raise


def _write_manifest(root: Path, body: bytes, name: str) -> None:
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest is too large (maximum {MAX_MANIFEST_BYTES} bytes)")
    descriptor, temporary = tempfile.mkstemp(prefix=".desktop-manifest-", suffix=".tmp", dir=root)
    pending = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, root / name)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        pending.unlink(missing_ok=True)
        raise


def _existing_asset(root: Path, name: str, size: int, digest: str) -> int | None:
    try:
        with _open_regular_entry(root, name) as source:
            info = os.fstat(source.fileno())
            if info.st_size != size or _hash_source(source) != digest:
                raise ValueError(
                    f"immutable release asset already exists with different contents: {name}")
            return getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
    except FileNotFoundError:
        return None


def publish(source: Path | str, version: str, *, expected_sha256: str | None = None,
            notes: str = "", directory: Path | str | None = None,
            platform: str | None = None, signature: str | None = None,
            public_key: bytes | None = None) -> dict:


    path = Path(source)
    platform = platform_for(path.name) if platform is None else _platform(platform).key
    target = _platform(platform)
    original_name = _file_name(path.name, platform)
    clean_version = release_version(
        _text(version, "--version", MAX_VERSION_BYTES, allow_empty=False).strip())
    clean_notes = _text(notes, "--notes", MAX_NOTES_BYTES, allow_empty=True)
    expected = _expected_digest(expected_sha256)
    clean_signature = None if signature is None else _signature_text(signature, "--signature")
    if clean_signature is not None and Path(original_name).suffix.lower() not in target.update_suffixes:
        raise ValueError(f"a {platform} release signature is only for the "
                         + " or ".join(sorted(target.update_suffixes))
                         + " update artifact; publish this file without --signature")
    if public_key is not None and clean_signature is None:
        raise ValueError("--public-key needs --signature")

    verify = (None if clean_signature is None
              else _ed25519_verifier(*_verification_key(public_key)))
    size, digest = _hash_file(path)
    if size <= 0:
        raise ValueError("release asset is empty")
    if expected is not None and digest != expected:
        raise ValueError(f"sha256 mismatch: expected {expected}, got {digest}")
    if verify is not None:

        verify(clean_signature, release_message(platform, clean_version, size, digest))

    name = _file_name(f"{digest[:16]}-{original_name}", platform)
    published_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    mtime_ns = int(time.time()) * 1_000_000_000
    release = {
        "version": clean_version, "file": name, "size": size, "sha256": digest,
        "platform": target.label, "signed": target.signed, "notarized": False,
        "published_at": published_at, "notes": clean_notes, "mtime_ns": mtime_ns,
    }
    if clean_signature is not None:
        release["signature"] = clean_signature
    manifest_body = _serialize_manifest(release, platform)

    root = downloads_dir(directory)
    root.mkdir(parents=True, exist_ok=True)
    with accounts.exclusive_file_lock(root / LOCK_NAME, timeout=LOCK_TIMEOUT_S):
        previous = _previous_name(root, platform)
        destination = root / name
        installed = False
        pending: Path | None = None
        try:
            existing_mtime = _existing_asset(root, name, size, digest)
            if existing_mtime is not None:
                release["mtime_ns"] = existing_mtime
                manifest_body = _serialize_manifest(release, platform)
            else:
                pending, copied_size, copied_digest = _copy_and_hash(path, root, mtime_ns)
                if copied_size != size or copied_digest != digest:
                    raise ValueError("release asset changed while it was being copied")
                os.replace(pending, destination)
                pending = None
                installed = True
                _fsync_directory(root)
            _write_manifest(root, manifest_body, target.manifest)
            _fsync_directory(root)
        except BaseException:
            if pending is not None:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    pass
            if installed:
                _remove_if_uncommitted(root, name)
            raise

        if previous and os.path.normcase(previous) != os.path.normcase(name):
            _remove_if_uncommitted(root, previous)
        with _HASH_LOCK:
            _HASH_CACHE.clear()
    return release


def _signature_state(key: str, release: dict) -> str:
    state = ("release signature present" if "signature" in release
             else "not signed - install manually (no auto-update)")
    return f"{key} {release['version']}: {state}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish_command = commands.add_parser(
        "publish", help="publish a macOS DMG/ZIP or a Windows installer EXE")
    publish_command.add_argument("path", type=Path)
    publish_command.add_argument("--version", required=True, help="MAJOR.MINOR.PATCH, e.g. 0.2.0")
    publish_command.add_argument("--sha256")
    publish_command.add_argument("--notes", default="")
    publish_command.add_argument("--platform", choices=sorted(PLATFORMS),
                                 help="must match the file type; inferred when omitted")
    publish_command.add_argument(
        "--signature", help="Ed25519 release signature: the .sig file or its base64 text; "
                            "it must verify against --public-key or the pinned key file")
    publish_command.add_argument(
        "--public-key", help="release public key (raw 32 bytes as base64, or a file holding it); "
                             f"required with --signature unless {DEFAULT_PUBLIC_KEY_FILE} "
                             f"(or ${PUBLIC_KEY_FILE_ENV}) pins it, and must match that pin")
    show_command = commands.add_parser("show", help="print the current manifests")
    show_command.add_argument("--platform", choices=sorted(PLATFORMS))
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            signature = None if args.signature is None else read_signature(args.signature)
            public_key = None if args.public_key is None else read_public_key(args.public_key)
            release = publish(args.path, args.version, expected_sha256=args.sha256,
                              notes=args.notes, platform=args.platform,
                              signature=signature, public_key=public_key)
            states = {}
            if signature is None:
                print("desktop release: warning: published without a release signature; "
                      "installed apps will not update to it, so it must be installed manually",
                      file=sys.stderr)
            else:
                source = ("--public-key" if public_key is not None
                          else f"the pinned release public key {pinned_public_key_file()[0]}")
                print(f"desktop release: release signature verified against {source}",
                      file=sys.stderr)
        elif args.platform:
            release = load(platform=args.platform)
            if release is None:
                print(f"No {args.platform} desktop build has been published on this server yet.",
                      file=sys.stderr)
                return 1
            states = {args.platform: release}
        else:
            release = load_all()
            if not release:
                print("No desktop build has been published on this server yet.", file=sys.stderr)
                return 1
            states = release
        print(json.dumps(release, indent=2, ensure_ascii=True))
        for key, item in states.items():
            print(_signature_state(key, item), file=sys.stderr)
        return 0
    except (OSError, ValueError) as exc:
        print(f"desktop release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
