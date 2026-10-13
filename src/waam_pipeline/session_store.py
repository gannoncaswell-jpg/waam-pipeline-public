from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat


class SessionStoreError(RuntimeError):
    def __init__(self):
        super().__init__("Sign-in is temporarily unavailable. Please try again shortly.")


_HEX = re.compile(r"[a-f0-9]{64}\Z")
_SID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
MAX_DATABASE_BYTES = 4 * 1024 * 1024


def sid_digest(sid: str | None) -> str | None:
    if not isinstance(sid, str) or not _SID.fullmatch(sid):
        return None
    return hashlib.sha256(sid.encode("ascii")).hexdigest()


class SessionStore:
    def __init__(self, path: Path, *, limit: int = 512, ttl: float = 43200):
        self.path = Path(path).absolute()
        self.limit = limit
        self.ttl = ttl
        self.authority: str | None = None

    def _check_parents(self) -> None:
        for parent in self.path.parents:
            if parent.is_symlink():
                raise SessionStoreError()

    def _check_files(self) -> None:
        self._check_parents()
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
                raise
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SessionStoreError()
            if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise SessionStoreError()
            if info.st_size > MAX_DATABASE_BYTES * (2 if suffix else 1):
                raise SessionStoreError()

    @contextmanager
    def _db(self, *, write: bool = False, check_authority: bool = True):
        con = None
        try:
            self._check_files()

            con = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                  timeout=1.0, isolation_level=None)
            con.execute("PRAGMA max_page_count=1024")
            if write:
                con.execute("BEGIN IMMEDIATE")
            if check_authority:
                row = con.execute("SELECT authority FROM session_meta WHERE id=1").fetchone()
                if row != (self.authority,):
                    raise SessionStoreError()
            yield con
            if write:
                con.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            raise SessionStoreError() from None
        finally:
            if con is not None:
                con.close()

    def set_authority(self, authority: str) -> None:
        if not _HEX.fullmatch(authority):
            raise SessionStoreError()
        if self.authority == authority:
            return
        if self.authority is None:
            try:
                self._check_parents()

                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            except OSError:
                raise SessionStoreError() from None
        with self._db(write=True, check_authority=False) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise SessionStoreError()
            con.execute("""CREATE TABLE IF NOT EXISTS session_meta (
                id INTEGER PRIMARY KEY CHECK(id=1), authority TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY CHECK(length(digest)=64),
                email TEXT NOT NULL CHECK(length(email) BETWEEN 3 AND 512),
                verified INTEGER NOT NULL CHECK(verified IN (0,1)),
                issued REAL NOT NULL, expires REAL NOT NULL,
                authority TEXT NOT NULL CHECK(length(authority)=64),
                credential TEXT NOT NULL CHECK(length(credential) IN (0,64)))""")
            old = con.execute("SELECT authority FROM session_meta WHERE id=1").fetchone()
            if old != (authority,):

                con.execute("DELETE FROM sessions")
                con.execute("INSERT OR REPLACE INTO session_meta VALUES (1,?)", (authority,))
            con.execute("PRAGMA user_version=1")
        self.authority = authority

    def create(self, sid: str, session: dict, now: float) -> None:
        digest = sid_digest(sid)
        authority = session["authority"]
        credential = session["credential"]
        if (digest is None or authority != self.authority
                or not isinstance(credential, str)
                or (credential and not _HEX.fullmatch(credential))
                or not math.isfinite(now) or not math.isfinite(session["expires"])
                or session["expires"] != now + self.ttl):
            raise SessionStoreError()
        with self._db(write=True) as con:
            con.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            excess = con.execute("SELECT count(*) FROM sessions").fetchone()[0] - self.limit + 1
            if excess > 0:
                con.execute("""DELETE FROM sessions WHERE digest IN (
                    SELECT digest FROM sessions ORDER BY expires,digest LIMIT ?)""", (excess,))
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                        (digest, session["email"], int(session["verified"]), now,
                         session["expires"], authority, credential))

    def get(self, sid: str, now: float) -> dict | None:
        digest = sid_digest(sid)
        if digest is None:
            return None
        with self._db() as con:
            row = con.execute("""SELECT email,verified,issued,expires,authority,credential
                                 FROM sessions WHERE digest=?""", (digest,)).fetchone()
        if row is None:
            return None
        email, verified, issued, expires, authority, credential = row
        if (not isinstance(email, str) or not 3 <= len(email) <= 512
                or type(verified) is not int or verified not in (0, 1)
                or not isinstance(issued, (int, float)) or not math.isfinite(issued)
                or not isinstance(expires, (int, float)) or not math.isfinite(expires)
                or expires != issued + self.ttl or authority != self.authority
                or not isinstance(credential, str)
                or (credential and not _HEX.fullmatch(credential))):
            raise SessionStoreError()
        if not issued <= now < expires:
            return None
        return {"email": email, "verified": bool(verified), "expires": expires,
                "authority": authority, "credential": credential}

    def drop(self, sid: str) -> bool:
        digest = sid_digest(sid)
        if digest is None:
            return False
        with self._db(write=True) as con:
            changed = con.execute("DELETE FROM sessions WHERE digest=?", (digest,)).rowcount
        return bool(changed)

    def drop_for(self, email: str) -> int:
        with self._db(write=True) as con:
            changed = con.execute("DELETE FROM sessions WHERE email=?", (email,)).rowcount
        return changed

    def clear(self) -> None:
        with self._db(write=True) as con:
            con.execute("DELETE FROM sessions")
