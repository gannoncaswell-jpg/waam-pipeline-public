from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

try:
    from webauthn import (generate_authentication_options,
                          generate_registration_options, options_to_json,
                          verify_authentication_response,
                          verify_registration_response)
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                          PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement,
                                          UserVerificationRequirement)
    _IMPORT_ERROR = None
except Exception as exc:
    _IMPORT_ERROR = exc


RP_NAME = "WAAM Studio"
CHALLENGE_TTL_S = 5 * 60
MAX_PENDING_CHALLENGES = 512
MAX_CREDENTIALS_PER_ACCOUNT = 10
PURPOSE_REGISTER = "register"
PURPOSE_LOGIN = "login"


def available() -> bool:

    return _IMPORT_ERROR is None


def _webauthn_file() -> Path:


    return Path(__file__).resolve().parents[2] / ".waam-webauthn.json"


def rp_id_and_origin(public_url: str) -> tuple[str, str]:


    u = urlparse(public_url or "")
    host = u.hostname or "localhost"
    origin = f"{u.scheme or 'https'}://{u.netloc}" if u.netloc else (public_url or "").rstrip("/")
    return host, origin


def _user_handle(email: str) -> bytes:


    return hashlib.sha256(("waam:" + email.strip().lower()).encode("utf-8")).digest()


@dataclass(frozen=True)
class Credential:
    id: str
    public_key: str
    sign_count: int
    label: str
    created: float
    last_used: float = 0.0
    transports: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "public_key": self.public_key,
                "sign_count": self.sign_count, "label": self.label,
                "created": self.created, "last_used": self.last_used,
                "transports": list(self.transports)}


class WebAuthnStore:


    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else _webauthn_file()
        self._lock = threading.Lock()

    @contextmanager
    def _writing(self):
        with self._lock:
            handle = None
            locked = False
            try:
                lock_path = self.path.with_name(self.path.name + ".lock")
                if lock_path.is_symlink():
                    raise WebAuthnError("Passkey store could not be updated.")
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(lock_path, flags, 0o600)
                handle = os.fdopen(fd, "r+b")
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WebAuthnError("Passkey store could not be updated.")
                deadline = time.monotonic() + 3.0
                while not locked:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            if not os.fstat(handle.fileno()).st_size:
                                handle.write(b"0"); handle.flush(); handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise WebAuthnError("Passkey store is busy. Try again shortly.") from None
                        time.sleep(0.02)
                yield
            except OSError:
                raise WebAuthnError("Passkey store could not be updated.") from None
            finally:
                if handle is not None:
                    if locked:
                        if os.name == "nt":
                            handle.seek(0); import msvcrt; msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl; fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()

    def _read(self) -> dict[str, list[Credential]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise WebAuthnError("The passkey store is unreadable. Contact the administrator.")
        if not isinstance(raw, dict) or not isinstance(raw.get("credentials", {}), dict):
            raise WebAuthnError("The passkey store is unreadable. Contact the administrator.")
        out: dict[str, list[Credential]] = {}
        for email, creds in (raw.get("credentials") or {}).items():
            items = []
            for rec in creds or ():
                try:
                    items.append(Credential(
                        id=str(rec["id"]), public_key=str(rec["public_key"]),
                        sign_count=int(rec.get("sign_count", 0)),
                        label=str(rec.get("label", "passkey")),
                        created=float(rec.get("created", 0.0)),
                        last_used=float(rec.get("last_used", 0.0)),
                        transports=tuple(rec.get("transports", []) or ())))
                except (KeyError, TypeError, ValueError):
                    continue
            if items:
                out[str(email).strip().lower()] = items
        return out

    def _write(self, data: dict[str, list[Credential]]) -> None:
        body = json.dumps(
            {"_what": "waam-pipeline WebAuthn passkeys. PUBLIC keys only, no "
                      "secrets. Machine-written: do not hand-edit.",
             "credentials": {e: [c.to_dict() for c in creds] for e, creds in data.items() if creds}},
            indent=2) + "\n"
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=self.path.name + ".", suffix=".tmp", delete=False) as out:
                tmp = Path(out.name)
                out.write(body); out.flush(); os.fsync(out.fileno())
            os.replace(tmp, self.path)
            tmp = None
            if os.name != "nt":
                dfd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def list_for(self, email: str) -> list[Credential]:
        with self._lock:
            return self._read().get((email or "").strip().lower(), [])

    def find(self, credential_id: str) -> tuple[str, Credential] | None:


        with self._lock:
            for email, creds in self._read().items():
                for c in creds:
                    if c.id == credential_id:
                        return email, c
        return None

    def add(self, email: str, cred: Credential) -> None:
        email = (email or "").strip().lower()
        with self._writing():
            data = self._read()
            creds = data.setdefault(email, [])
            if any(c.id == cred.id for c in creds):
                raise WebAuthnError("That passkey is already registered.")
            if len(creds) >= MAX_CREDENTIALS_PER_ACCOUNT:
                raise WebAuthnError("Too many passkeys on this account. Remove one first.")
            creds.append(cred)
            self._write(data)

    def record_use(self, email: str, credential_id: str, sign_count: int) -> None:

        email = (email or "").strip().lower()
        with self._writing():
            data = self._read()
            creds = data.get(email, [])
            for i, c in enumerate(creds):
                if c.id == credential_id:
                    creds[i] = Credential(**{**c.to_dict(),
                                             "sign_count": int(sign_count),
                                             "last_used": time.time(),
                                             "transports": tuple(c.transports)})
                    self._write(data)
                    return

    def remove(self, email: str, credential_id: str) -> bool:
        email = (email or "").strip().lower()
        with self._writing():
            data = self._read()
            creds = data.get(email, [])
            kept = [c for c in creds if c.id != credential_id]
            if len(kept) == len(creds):
                return False
            if kept:
                data[email] = kept
            else:
                data.pop(email, None)
            self._write(data)
            return True


class WebAuthnError(Exception):
    pass


_LOCK = threading.Lock()
_CHALLENGES: dict[str, dict] = {}


def _prune(now: float) -> None:
    for h in [h for h, c in _CHALLENGES.items() if c["expires"] < now]:
        del _CHALLENGES[h]
    if len(_CHALLENGES) > MAX_PENDING_CHALLENGES:
        for h in sorted(_CHALLENGES, key=lambda h: _CHALLENGES[h]["expires"])[:len(_CHALLENGES) // 2]:
            del _CHALLENGES[h]


def _stash(challenge: bytes, purpose: str, email: str | None) -> str:
    handle = secrets.token_urlsafe(24)
    now = time.time()
    with _LOCK:
        _prune(now)
        _CHALLENGES[handle] = {"challenge": challenge, "purpose": purpose,
                               "email": email, "expires": now + CHALLENGE_TTL_S}
    return handle


def _take(handle: str, purpose: str) -> tuple[bytes, str | None]:
    now = time.time()
    with _LOCK:
        _prune(now)
        entry = _CHALLENGES.pop(str(handle or ""), None)
    if entry is None or entry["purpose"] != purpose:
        raise WebAuthnError("This passkey request expired. Start again.")
    return entry["challenge"], entry["email"]


def forget_challenges() -> None:

    with _LOCK:
        _CHALLENGES.clear()


def registration_options(public_url: str, email: str, existing: list[Credential],
                         *, require_uv: bool = False) -> tuple[str, str]:


    rp_id, _origin = rp_id_and_origin(public_url)
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_name=email, user_id=_user_handle(email),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.id))
                             for c in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=(UserVerificationRequirement.REQUIRED if require_uv
                               else UserVerificationRequirement.PREFERRED)),
    )
    handle = _stash(opts.challenge, PURPOSE_REGISTER, email)
    return handle, options_to_json(opts)


def verify_registration(public_url: str, handle: str, credential: str, label: str,
                        *, require_uv: bool = False) -> tuple[str, Credential]:

    challenge, email = _take(handle, PURPOSE_REGISTER)
    rp_id, origin = rp_id_and_origin(public_url)
    try:
        v = verify_registration_response(
            credential=credential, expected_challenge=challenge,
            expected_rp_id=rp_id, expected_origin=origin,
            require_user_verification=require_uv)
    except Exception as exc:
        raise WebAuthnError(
            "That passkey could not be verified. An administrator's passkey "
            "must ask for a PIN, fingerprint or face." if require_uv
            else "That passkey could not be verified.") from exc
    cred = Credential(
        id=bytes_to_base64url(v.credential_id),
        public_key=bytes_to_base64url(v.credential_public_key),
        sign_count=v.sign_count, label=(label or "passkey")[:40], created=time.time())
    return email, cred


def authentication_options(public_url: str, allow: list[Credential] | None = None) -> tuple[str, str]:


    rp_id, _origin = rp_id_and_origin(public_url)
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.id))
                           for c in (allow or [])] or None,
        user_verification=UserVerificationRequirement.PREFERRED)
    handle = _stash(opts.challenge, PURPOSE_LOGIN, None)
    return handle, options_to_json(opts)


def verify_authentication(public_url: str, handle: str, credential: str,
                          cred: Credential, *, require_uv: bool = False) -> int:


    challenge, _email = _take(handle, PURPOSE_LOGIN)
    rp_id, origin = rp_id_and_origin(public_url)
    try:
        v = verify_authentication_response(
            credential=credential, expected_challenge=challenge,
            expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=base64url_to_bytes(cred.public_key),
            credential_current_sign_count=cred.sign_count,
            require_user_verification=require_uv)
    except Exception as exc:
        raise WebAuthnError(
            "Passkey sign-in could not be verified. An administrator's passkey "
            "must ask for a PIN, fingerprint or face." if require_uv
            else "Passkey sign-in could not be verified.") from exc
    return v.new_sign_count


def credential_id_from(credential: str) -> str:

    try:
        data = json.loads(credential) if isinstance(credential, str) else credential
        return str(data["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WebAuthnError("Malformed passkey response.") from exc
