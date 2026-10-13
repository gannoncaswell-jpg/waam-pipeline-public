from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .accounts import MAX_ACCOUNTS
from .mailer import MailConfig
from .session_store import SessionStore, SessionStoreError


GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

MS_AUTHORIZE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MS_ISSUER = "https://login.microsoftonline.com/{tid}/v2.0"


MS_CONSUMER_TID = "9188040d-6c67-4c5b-b112-36a304b66dad"


MS_MULTI_TENANT = ("common", "organizations", "consumers")

SCOPE = "openid email profile"


_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+!#$&*/=?^`{|}~-]{1,128}"
                       r"@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")


PENDING_TTL_S = 600
MAX_PENDING = 256


SESSION_TTL_S = 12 * 3600
MAX_SESSIONS = 512


CLOCK_LEEWAY_S = 120

_PENDING: dict[str, dict] = {}
_SESSIONS: dict[str, dict] = {}
_SESSION_DATABASE: SessionStore | None = None
_SESSION_AUTHORITY: str | None = None
_LOCK = threading.Lock()


class AuthError(Exception):
    pass


@dataclass
class AuthConfig:
    provider: str
    client_id: str
    client_secret: str
    public_url: str
    tenant: str = "common"
    users: dict[str, dict] = field(default_factory=dict)
    allow_domains: tuple[str, ...] = ()
    blocked: frozenset[str] = frozenset()


    open_signin: bool = True


    issuer: str = ""
    authorize_url: str = ""
    token_url: str = ""
    sign_in_label: str = ""


    mail: MailConfig | None = None


    approval_required: bool = False
    approved: frozenset[str] = frozenset()
    approval_notify: str = ""


    admins: frozenset[str] = frozenset()


    max_accounts: int = MAX_ACCOUNTS

    @property
    def verify_email(self) -> bool:


        return self.local and self.mail is not None

    @property
    def local(self) -> bool:


        return self.provider == "local"

    @property
    def label(self) -> str:


        if self.sign_in_label:
            return self.sign_in_label
        return {"google": "Google", "microsoft": "Microsoft",
                "local": ""}.get(self.provider, "your account")

    @property
    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/auth/callback"

    @property
    def authorize_endpoint(self) -> str:
        if self.provider == "google":
            return GOOGLE_AUTHORIZE
        if self.provider == "microsoft":
            return MS_AUTHORIZE.format(tenant=self.tenant)
        return self.authorize_url

    @property
    def token_endpoint(self) -> str:
        if self.provider == "google":
            return GOOGLE_TOKEN
        if self.provider == "microsoft":
            return MS_TOKEN.format(tenant=self.tenant)
        return self.token_url

    @property
    def pinned_tenant(self) -> str | None:

        if self.provider != "microsoft" or self.tenant in MS_MULTI_TENANT:
            return None
        return self.tenant


def _auth_config_file() -> Path:

    return Path(__file__).resolve().parents[2] / ".waam-auth.json"


def _is_local_origin(url: str) -> bool:


    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def load_auth_config(path: Path | None = None) -> AuthConfig | None:


    f = path or _auth_config_file()
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  {f.name} unreadable ({exc.__class__.__name__}) - sign-in disabled")
        return None
    if not isinstance(raw, dict):
        print(f"  {f.name} is not a JSON object - sign-in disabled")
        return None

    provider = str(raw.get("provider", "")).strip().lower()
    client_id = str(raw.get("client_id", "")).strip()
    client_secret = str(raw.get("client_secret", "")).strip()
    public_url = str(raw.get("public_url", "")).strip()
    tenant = str(raw.get("tenant", "common")).strip() or "common"

    issuer = str(raw.get("issuer", "")).strip().rstrip("/")
    authorize_url = str(raw.get("authorize_endpoint", "")).strip()
    token_url = str(raw.get("token_endpoint", "")).strip()
    sign_in_label = " ".join(str(raw.get("sign_in_label", "")).split())[:60]

    problems = []
    if provider not in ("google", "microsoft", "oidc", "local"):
        problems.append(
            'provider must be "google", "microsoft", "oidc" or "local"')
    if provider == "local":


        client_id = client_id or "local"
        client_secret = client_secret or "local"
    if provider == "oidc":


        for key, value in (("issuer", issuer),
                           ("authorize_endpoint", authorize_url),
                           ("token_endpoint", token_url)):
            if not value:
                problems.append(f'provider "oidc" needs {key}')
            elif not value.startswith("https://"):


                problems.append(f"{key} must be an https URL")
    if not client_id:
        problems.append("client_id is empty")
    if not client_secret:
        problems.append("client_secret is empty")
    if not public_url.startswith(("https://", "http://")):
        problems.append("public_url must be an absolute http(s) URL")
    elif public_url.startswith("http://") and not _is_local_origin(public_url):


        problems.append(
            "public_url is plain http on a non-loopback host, so the browser "
            "will drop the Secure sign-in cookies and the sign-in will loop "
            "silently. Use the https Funnel URL (see docs/sso-setup.md)"
        )
    if provider == "microsoft" and not _TENANT_RE.fullmatch(tenant):
        problems.append("tenant must be a GUID, a domain, or common/organizations")

    users: dict[str, dict] = {}
    for email, info in (raw.get("users") or {}).items():
        key = str(email).strip().lower()
        if not _EMAIL_RE.fullmatch(key):
            problems.append(f"users key {email!r} is not an email address")
            continue
        users[key] = info if isinstance(info, dict) else {}

    domains = []
    for d in (raw.get("allow_domains") or []):
        clean = str(d).strip().lower().lstrip("@")
        if clean:
            domains.append(clean)


    blocked = {str(b).strip().lower() for b in (raw.get("blocked") or []) if str(b).strip()}

    mail = MailConfig.load(raw.get("mail"))
    if mail is not None:


        problems.extend(mail.validation_errors())

    open_signin = raw.get("open_signin", True)
    if not isinstance(open_signin, bool):
        problems.append("open_signin must be true or false")

    approval_required = raw.get("approval_required", False)
    if not isinstance(approval_required, bool):
        problems.append("approval_required must be true or false")
    approved = {str(a).strip().lower() for a in (raw.get("approved") or [])
                if str(a).strip()}
    approval_notify = str(raw.get("approval_notify") or "").strip().lower()
    if approval_notify and not _EMAIL_RE.fullmatch(approval_notify):
        problems.append("approval_notify must be an email address")


    admins = {str(a).strip().lower() for a in (raw.get("admins") or [])
              if str(a).strip()}
    for a in sorted(admins):
        if not _EMAIL_RE.fullmatch(a):
            problems.append(f"admins entry {a!r} is not an email address")

    max_accounts = raw.get("max_accounts", MAX_ACCOUNTS)
    if isinstance(max_accounts, bool) or not isinstance(max_accounts, int) or max_accounts < 1:
        problems.append("max_accounts must be a whole number of at least 1")

    if problems:
        print(f"  {f.name}: {'; '.join(problems)} - sign-in disabled")
        return None
    if not users and not domains and not open_signin:
        print(f"  {f.name} sets open_signin false but lists no users and no "
              "allow_domains, so it would refuse everyone - sign-in disabled")
        return None
    return AuthConfig(
        provider=provider, client_id=client_id, client_secret=client_secret,
        public_url=public_url, tenant=tenant, users=users,
        allow_domains=tuple(domains), blocked=frozenset(blocked),
        open_signin=bool(open_signin), issuer=issuer,
        authorize_url=authorize_url, token_url=token_url,
        sign_in_label=sign_in_label, mail=mail,
        approval_required=bool(approval_required),
        approved=frozenset(approved), approval_notify=approval_notify,
        admins=frozenset(admins), max_accounts=int(max_accounts),
    )


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _same(claimed, expected: str) -> bool:


    if not isinstance(claimed, str) or not claimed:
        return False
    return secrets.compare_digest(claimed.encode("utf-8", "surrogatepass"),
                                  expected.encode("utf-8"))


def _prune_pending(now: float, reserve: int = 0) -> None:

    for state in [s for s, p in _PENDING.items() if p["created"] + PENDING_TTL_S < now]:
        _PENDING.pop(state, None)


    while len(_PENDING) + reserve > MAX_PENDING:
        oldest = min(_PENDING, key=lambda s: _PENDING[s]["created"])
        _PENDING.pop(oldest, None)


def begin_login(cfg: AuthConfig) -> tuple[str, str]:


    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    binding = secrets.token_urlsafe(24)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    now = time.time()
    with _LOCK:
        _prune_pending(now, reserve=1)
        _PENDING[state] = {"nonce": nonce, "verifier": verifier,
                           "binding": binding, "created": now}
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",


        "prompt": "select_account",
    }
    return cfg.authorize_endpoint + "?" + urllib.parse.urlencode(params), binding


def binding_ok(pending: dict, presented: str | None) -> bool:

    return _same(presented, pending.get("binding", ""))


def take_pending(state: str) -> dict | None:

    if not state:
        return None
    now = time.time()
    with _LOCK:
        _prune_pending(now)
        pending = _PENDING.pop(state, None)
    if pending is None or pending["created"] + PENDING_TTL_S < now:
        return None
    return pending


class _NoTokenRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):

        return None


def _post_form(url: str, data: dict) -> dict:


    endpoint = urllib.parse.urlsplit(url)
    if endpoint.scheme != "https" or not endpoint.hostname:
        raise ValueError("token endpoint must be an absolute https URL")
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoTokenRedirects)
    with opener.open(req, timeout=15) as resp:
        raw = resp.read(1 << 20)
    return json.loads(raw.decode("utf-8"))


def exchange_code(cfg: AuthConfig, code: str, verifier: str) -> str:


    try:
        payload = _post_form(cfg.token_endpoint, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code_verifier": verifier,
        })
    except urllib.error.HTTPError as exc:
        raise AuthError(f"the provider refused the sign-in (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise AuthError(f"could not reach {cfg.label} ({exc.reason})") from None
    except (ValueError, OSError):
        raise AuthError("the provider's reply was not readable JSON") from None
    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise AuthError("the provider returned no ID token")
    return id_token


def id_token_claims(id_token: str) -> dict:


    if not isinstance(id_token, str) or len(id_token) > 8192:
        raise AuthError("malformed ID token")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed ID token")
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise AuthError("malformed ID token") from None
    if not isinstance(claims, dict):
        raise AuthError("malformed ID token")
    return claims


def validate_claims(cfg: AuthConfig, claims: dict, nonce: str) -> None:

    iss = str(claims.get("iss", ""))
    if cfg.provider == "google":
        if iss not in GOOGLE_ISSUERS:
            raise AuthError("ID token came from the wrong issuer")
    elif cfg.provider == "oidc":


        if iss.rstrip("/") != cfg.issuer.rstrip("/"):
            raise AuthError("ID token came from the wrong issuer")
    else:
        tid = str(claims.get("tid", ""))
        if not tid or MS_ISSUER.format(tid=tid) != iss:
            raise AuthError("ID token came from the wrong issuer")
        pinned = cfg.pinned_tenant
        if pinned and tid.lower() != pinned.lower():
            raise AuthError("ID token came from a different Microsoft tenant")

    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not any(_same(a, cfg.client_id) for a in auds):
        raise AuthError("ID token was issued for a different application")

    now = time.time()
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        raise AuthError("ID token has no expiry") from None
    if exp + CLOCK_LEEWAY_S < now:
        raise AuthError("ID token has expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and float(nbf) - CLOCK_LEEWAY_S > now:
        raise AuthError("ID token is not valid yet")

    if not _same(claims.get("nonce"), nonce):
        raise AuthError("ID token does not answer this sign-in")


def email_of(cfg: AuthConfig, claims: dict) -> tuple[str, bool]:


    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        upn = claims.get("preferred_username")
        email = upn if isinstance(upn, str) and "@" in upn else ""
    email = email.strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        return "", False

    raw = claims.get("email_verified")
    if isinstance(raw, str):
        verified = raw.strip().lower() == "true"
    else:
        verified = bool(raw)
    if not verified and cfg.pinned_tenant:
        tid = str(claims.get("tid", "")).lower()
        verified = bool(tid) and tid != MS_CONSUMER_TID
    return email, verified


def session_authority(cfg: AuthConfig | None) -> str:

    fields = None if cfg is None else [
        cfg.provider, cfg.public_url, cfg.client_id, cfg.tenant,
        cfg.issuer, cfg.authorize_url, cfg.token_url]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def enable_session_persistence(path: Path | None) -> None:


    global _SESSION_DATABASE, _SESSION_AUTHORITY
    with _LOCK:
        _SESSION_DATABASE = (SessionStore(path, limit=MAX_SESSIONS, ttl=SESSION_TTL_S)
                             if path is not None else None)
        _SESSION_AUTHORITY = None
        _SESSIONS.clear()
        _PENDING.clear()


def set_session_authority(cfg: AuthConfig | None) -> None:
    global _SESSION_AUTHORITY
    authority = session_authority(cfg)
    with _LOCK:
        if _SESSION_DATABASE is not None:
            _SESSION_DATABASE.set_authority(authority)
        if _SESSION_AUTHORITY is not None and _SESSION_AUTHORITY != authority:
            _SESSIONS.clear()
            _PENDING.clear()
        _SESSION_AUTHORITY = authority


def _prune_sessions(now: float, reserve: int = 0) -> None:

    for sid in [s for s, v in _SESSIONS.items() if v["expires"] <= now]:
        _SESSIONS.pop(sid, None)
    while len(_SESSIONS) + reserve > MAX_SESSIONS:
        oldest = min(_SESSIONS, key=lambda s: _SESSIONS[s]["expires"])
        _SESSIONS.pop(oldest, None)


def new_session(email: str, verified: bool, *, authority: str = "",
                credential: str = "") -> str:


    sid = secrets.token_urlsafe(32)
    now = time.time()
    if not isinstance(email, str) or len(email) > 512 or not _EMAIL_RE.fullmatch(email):
        raise SessionStoreError()
    session = {"email": email, "verified": bool(verified),
               "expires": now + SESSION_TTL_S,
               "authority": authority, "credential": credential}
    with _LOCK:
        if _SESSION_DATABASE is not None:
            _SESSION_DATABASE.create(sid, session, now)
        else:
            _prune_sessions(now, reserve=1)
            _SESSIONS[sid] = session
    return sid


def session_for(sid: str | None) -> dict | None:


    if not sid:
        return None
    now = time.time()
    try:
        probe = sid.encode("utf-8", "surrogatepass")
    except (AttributeError, UnicodeError):
        return None
    with _LOCK:
        if _SESSION_DATABASE is not None:
            session = _SESSION_DATABASE.get(sid, now)
            if session is not None and not _EMAIL_RE.fullmatch(session["email"]):
                raise SessionStoreError()
            return session
        _prune_sessions(now)
        for known, sess in _SESSIONS.items():
            if secrets.compare_digest(probe, known.encode("utf-8")):
                return dict(sess)
    return None


def drop_session(sid: str | None) -> bool:

    if not sid:
        return False
    with _LOCK:
        if _SESSION_DATABASE is not None:
            return _SESSION_DATABASE.drop(sid)
        _prune_sessions(time.time())
        try:
            probe = sid.encode("utf-8", "surrogatepass")
        except (AttributeError, UnicodeError):
            return False
        for known in list(_SESSIONS):
            if secrets.compare_digest(probe, known.encode("utf-8")):
                _SESSIONS.pop(known, None)
                return True
    return False


def drop_sessions_for(email: str) -> int:


    email = (email or "").strip().lower()
    with _LOCK:
        if _SESSION_DATABASE is not None:
            return _SESSION_DATABASE.drop_for(email)
        doomed = [sid for sid, sess in _SESSIONS.items()
                  if sess.get("email") == email]
        for sid in doomed:
            del _SESSIONS[sid]
    return len(doomed)


def forget_everything() -> None:

    with _LOCK:
        if _SESSION_DATABASE is not None:
            _SESSION_DATABASE.clear()
        _PENDING.clear()
        _SESSIONS.clear()
