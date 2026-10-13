from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


MAX_BYTES = 4096
DEFAULTS = {
    "theme": "graphite",
    "density": "comfortable",
    "reduce_motion": False,
    "default_frame": "cell",
    "default_camera": "iso",
    "path_only": False,
}
_CHOICES = {
    "theme": ("graphite", "drafting", "operator"),
    "density": ("comfortable", "compact"),
    "default_frame": ("cell", "part"),
    "default_camera": ("iso", "top"),
}


_IO_LOCK = threading.RLock()


class InvalidPreferences(ValueError):
    pass


class PreferencesUnavailable(RuntimeError):
    pass


def owner_id(email: str) -> str:


    value = email.strip().lower() if isinstance(email, str) else ""
    if not value or len(value) > 254 or "@" not in value or any(ord(c) < 32 for c in value):
        raise ValueError("A signed-in account is required for preferences.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate(value: object) -> dict:

    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise InvalidPreferences("Send all six appearance and 3D preview preferences only.")
    for key, choices in _CHOICES.items():
        if not isinstance(value[key], str) or value[key] not in choices:
            raise InvalidPreferences(f"Invalid {key.replace('_', ' ')} preference.")
    for key in ("reduce_motion", "path_only"):
        if type(value[key]) is not bool:
            raise InvalidPreferences(f"{key.replace('_', ' ').capitalize()} must be true or false.")
    return {key: value[key] for key in DEFAULTS}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _json(body: bytes):
    if not body or len(body) > MAX_BYTES:
        raise ValueError("Invalid preference document size")
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)


def decode(body: bytes) -> dict:
    try:
        value = _json(body)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidPreferences("Send a valid JSON object of UI preferences.") from None
    return validate(value)


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = (Path(directory) if directory is not None else
                          Path(__file__).resolve().parents[2] / ".waam-ui-preferences")

    def _check_directory(self):
        if self.directory.is_symlink():
            raise OSError("Preference directory must not be a symlink")

    def _read(self, identity: str) -> dict:
        self._check_directory()
        path = self.directory / (identity + ".json")
        if path.is_symlink():
            raise OSError("Preference file must not be a symlink")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return dict(DEFAULTS)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())


            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > MAX_BYTES:
                raise ValueError("Invalid preference file")
            record = _json(source.read(MAX_BYTES + 1))
        if (not isinstance(record, dict) or set(record) != {"version", "owner_id", "preferences"}
                or type(record["version"]) is not int or record["version"] != 1
                or record["owner_id"] != identity):
            raise ValueError("Invalid preference record")
        return validate(record["preferences"])

    def get(self, email: str) -> dict:
        identity = owner_id(email)
        try:
            with _IO_LOCK:
                return self._read(identity)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise PreferencesUnavailable(
                "Your display preferences could not be read. Retry or contact the administrator.") from None

    def save(self, email: str, preferences: object) -> dict:
        with _IO_LOCK:
            return self._save(email, preferences)

    def _save(self, email: str, preferences: object) -> dict:
        identity, clean = owner_id(email), validate(preferences)
        temporary = None
        try:

            self._read(identity)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_directory()
            target = self.directory / (identity + ".json")
            body = json.dumps({"version": 1, "owner_id": identity, "preferences": clean},
                              sort_keys=True, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.directory,
                    prefix=identity + ".", suffix=".tmp", delete=False) as out:
                temporary = Path(out.name)
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            temporary = None


            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise PreferencesUnavailable(
                "Your display preferences could not be saved. Keep this page open and retry.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return clean
