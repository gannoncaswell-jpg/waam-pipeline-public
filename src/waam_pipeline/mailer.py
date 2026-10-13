from __future__ import annotations

import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr


TIMEOUT_S = 20


STARTTLS_PORTS = (587, 2525)
IMPLICIT_TLS_PORTS = (465,)


class MailError(RuntimeError):
    pass


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str

    @property
    def implicit_tls(self) -> bool:
        return self.port in IMPLICIT_TLS_PORTS

    def validation_errors(self) -> list[str]:
        e: list[str] = []
        if not self.host.strip():
            e.append("mail.host is empty")
        if self.port not in STARTTLS_PORTS + IMPLICIT_TLS_PORTS:
            e.append(
                f"mail.port {self.port} is not a submission port this knows how "
                f"to secure ({', '.join(str(p) for p in STARTTLS_PORTS)} use "
                f"STARTTLS, {IMPLICIT_TLS_PORTS[0]} is direct TLS). Port 25 is "
                "plaintext relay and is refused.")
        if not self.username.strip():
            e.append("mail.username is empty")
        if not self.password:
            e.append("mail.password is empty (for Gmail this is a 16-character "
                     "APP PASSWORD, not the account password)")
        if "@" not in parseaddr(self.sender)[1]:
            e.append("mail.from is not an email address")
        return e

    @classmethod
    def load(cls, raw: dict | None) -> "MailConfig | None":


        if not isinstance(raw, dict) or not raw:
            return None
        username = str(raw.get("username", "")).strip()
        return cls(
            host=str(raw.get("host", "")).strip(),
            port=int(raw.get("port", 587) or 587),
            username=username,
            password=str(raw.get("password", "")),
            sender=str(raw.get("from") or raw.get("sender") or username).strip(),
        )


MAX_SENDS_PER_WINDOW = 100
SEND_WINDOW_S = 60 * 60
_SENDS: list[float] = []
_SEND_LOCK = threading.Lock()


def _take_send_budget() -> bool:
    now = time.monotonic()
    with _SEND_LOCK:
        _SENDS[:] = [t for t in _SENDS if now - t < SEND_WINDOW_S]
        if len(_SENDS) >= MAX_SENDS_PER_WINDOW:
            return False
        _SENDS.append(now)
        return True


def forget_send_budget() -> None:

    with _SEND_LOCK:
        _SENDS.clear()


def _connect(cfg: MailConfig):
    context = ssl.create_default_context()
    if cfg.implicit_tls:
        return smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=TIMEOUT_S,
                                context=context)
    server = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT_S)
    server.ehlo()
    server.starttls(context=context)
    server.ehlo()
    return server


def send(cfg: MailConfig, to: str, subject: str, body: str) -> None:

    problems = cfg.validation_errors()
    if problems:
        raise MailError("the mail settings are incomplete: " + problems[0])
    if not _take_send_budget():
        raise MailError(
            "this workbench has already sent as many messages as it will in an "
            "hour. Try again later, or contact the administrator.")

    msg = EmailMessage()
    name, addr = parseaddr(cfg.sender)
    msg["From"] = formataddr((name or "WAAM Studio", addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with _connect(cfg) as server:
            server.login(cfg.username, cfg.password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:


        raise MailError(
            "the mail server rejected the sign-in for this workbench's own "
            "email account. For Gmail, mail.password must be a 16-character "
            "app password with 2-step verification switched on."
        ) from exc
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:

        raise MailError(
            f"could not send the email ({exc.__class__.__name__}). Check "
            "mail.host and mail.port, and that this machine can reach them."
        ) from exc


def send_code(cfg: MailConfig, to: str, code: str, *, url: str = "") -> None:

    where = f"\n\n    {url}\n" if url else "\n"
    send(cfg, to, "Your WAAM Studio code", f"""\
Your code is:

    {code}

Type it into the page you just came from to finish setting up your account.{where}
The code works for 15 minutes and only once.

If you did not ask for this, you can ignore it -- nobody can get in with
just your email address, and no account was created for anyone else.
""")


def send_reset_code(cfg: MailConfig, to: str, code: str, *, url: str = "") -> None:


    where = f"\n\n    {url}\n" if url else "\n"
    send(cfg, to, "Your WAAM Studio reset code", f"""\
Somebody asked to set a new password for the WAAM Studio account on this
address. Your code is:

    {code}

Type it into the page you came from, along with the new password you want.{where}
The code works for 15 minutes and only once.

If this was not you, nothing has happened and nothing will: your password is
unchanged and nobody can set a new one without this code. You can ignore this
email.
""")


def _configure(path, example) -> int:


    import getpass
    import json
    import shutil
    from email.utils import parseaddr

    if not path.exists():
        if not example.exists():
            print(f"neither {path.name} nor {example.name} exists. See "
                  "docs/sso-setup.md.")
            return 1
        print(f"{path.name} does not exist yet.")
        print(f"Creating it from {example.name} SWITCHES SIGN-IN ON: from then")
        print("on the workbench offers sign in / create an account / guest.")
        print("Existing ?k= links keep working either way.")
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing changed.")
            return 1
        shutil.copy2(example, path)
        print(f"created {path.name}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{path.name} is unreadable ({exc.__class__.__name__}); not "
              "touching it.")
        return 1

    current = (raw.get("mail") or {}).get("username", "")
    if "FILL-IN" in str(current).upper():
        current = ""
    prompt = f"sending Gmail address [{current}]: " if current else \
             "sending Gmail address: "
    address = input(prompt).strip() or current
    if "@" not in parseaddr(address)[1]:
        print("that does not look like an email address; nothing changed.")
        return 1

    print("\nApp password: 16 characters, from the Google account's Security")
    print("page with 2-step verification on. NOT your Google password.")
    print("It will not be shown as you type.")
    password = getpass.getpass("app password: ").replace(" ", "")
    if not password:
        print("nothing entered; nothing changed.")
        return 1

    mail = dict(raw.get("mail") or {})
    mail.update(host=mail.get("host") or "smtp.gmail.com",
                port=int(mail.get("port") or 587),
                username=address, password=password)
    name = parseaddr(mail.get("from", ""))[0]
    mail["from"] = formataddr((name or "WAAM Studio", address))
    raw["mail"] = mail

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(f"\nwrote {path.name}: sending as {mail['from']} via "
          f"{mail['host']}:{mail['port']},")
    print(f"app password stored ({len(password)} characters). It is not "
          "printed here or anywhere else.")
    print("\nNow prove it actually sends:")
    print(f"    python -m waam_pipeline.mailer --test {address}")
    print("    python -m waam_pipeline.authcheck")
    return 0


def _main(argv=None) -> int:
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="python -m waam_pipeline.mailer",
        description="Configure and test the workbench's outgoing email.")
    ap.add_argument("--configure", action="store_true",
                    help="prompt for the sending address and app password "
                         "(never echoed) and write them into .waam-auth.json")
    ap.add_argument("--test", metavar="ADDRESS", default=None,
                    help="send a test code to this address")
    ap.add_argument("--file", type=Path, default=None,
                    help="use this config file instead of .waam-auth.json")
    args = ap.parse_args(argv)

    from . import oidc as _oidc

    if args.configure:
        target = args.file or _oidc._auth_config_file()
        return _configure(target, target.with_suffix(".json.example"))
    if not args.test:
        ap.error("give --configure or --test ADDRESS")

    from . import oidc

    path = args.file or oidc._auth_config_file()
    if not path.exists():
        print(f"{path.name} does not exist, so there are no mail settings. "
              "See docs/sso-setup.md.")
        return 1
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{path.name} is unreadable: {exc.__class__.__name__}")
        return 1
    cfg = MailConfig.load(raw.get("mail"))
    if cfg is None:
        print(f"{path.name} has no `mail` block, so email verification is OFF "
              "and registrations are never proven. See docs/sso-setup.md.")
        return 1
    problems = cfg.validation_errors()
    if problems:
        for line in problems:
            print(f"FAIL: {line}")
        return 1

    print(f"sending as {cfg.sender} via {cfg.host}:{cfg.port} ...")
    try:
        send_code(cfg, args.test, "123456")
    except MailError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"sent. Check {args.test} (and the spam folder). The code in it is "
          "a fixed 123456 and means nothing.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
