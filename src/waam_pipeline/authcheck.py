'Check the workbench sign-in configuration before anyone tries to use it.'


from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import oidc


TEMPLATE = {
    "provider": "microsoft",
    "client_id": "<Application (client) ID from the app registration>",
    "client_secret": "<the secret VALUE, not the Secret ID - shown once>",
    "tenant": "<Directory (tenant) ID - a GUID, not 'common'>",
    "public_url": "https://<your-funnel-host>:8443",
    "users": {"you@letu.edu": {"name": "you"}},
    "allow_domains": ["letu.edu"],
    "blocked": [],
    "open_signin": True,
    "sign_in_label": "your LeTourneau account",
}


TEMPLATE_OIDC = {
    "provider": "oidc",
    "sign_in_label": "your LeTourneau account",
    "issuer": "https://<the provider's issuer URL>",
    "authorize_endpoint": "https://<...>/authorize",
    "token_endpoint": "https://<...>/token",
    "client_id": "<from the app registration>",
    "client_secret": "<from the app registration>",
    "public_url": "https://<your-funnel-host>:8443",
    "users": {"you@letu.edu": {"name": "you"}},
    "allow_domains": ["letu.edu"],
    "blocked": [],
    "open_signin": True,
}


MS_CONSOLE = "https://portal.azure.com"
MS_CONSOLE_ALT = "https://entra.microsoft.com"
GOOGLE_CONSOLE = "https://console.cloud.google.com"


def _public_url(path: Path | None) -> tuple[str, str]:


    for candidate, note in ((path, "--file"),
                            (oidc._auth_config_file(), ".waam-auth.json"),
                            (oidc._auth_config_file().with_suffix(
                                ".json.example"), ".waam-auth.json.example")):
        if candidate is None or not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            url = str(raw.get("public_url", "")).strip().rstrip("/")
        except (OSError, ValueError):
            continue
        if url.startswith(("https://", "http://")):
            return url, note
    return "https://<your-funnel-host>:8443", "not found - fill this in yourself"


def setup_steps(path: Path | None = None) -> list[str]:


    url, src = _public_url(path)
    redirect = url + "/auth/callback"
    return [
        "REGISTERING THE SIGN-IN APPLICATION",
        "",
        f"Your redirect URI (public_url from {src}):",
        "",
        f"    {redirect}",
        "",
        "It must match CHARACTER FOR CHARACTER in the console and in",
        ".waam-auth.json. Copy it; do not retype it. If the Funnel URL ever",
        "changes, this changes with it in both places.",
        "",
        "-- MICROSOFT (the likely one - the team is on LeTourneau 365) -------",
        "",
        f"  1. {MS_CONSOLE}  (or {MS_CONSOLE_ALT})",
        "     Microsoft Entra ID -> App registrations -> New registration",
        "  2. Name:  WAAM Workbench",
        "  3. Supported account types:",
        "         Accounts in this organizational directory only",
        "     This is the strongest lever you have. Microsoft itself then",
        "     refuses every non-LeTourneau account before it ever reaches",
        "     the workbench.",
        "  4. Redirect URI:  platform Web, value = the line above",
        "  5. Register. From the Overview page, copy into .waam-auth.json:",
        "         Application (client) ID   -> client_id",
        "         Directory (tenant) ID     -> tenant     (a GUID, NOT 'common')",
        "  6. Certificates & secrets -> New client secret -> copy the VALUE",
        "     immediately (it is shown once, and it is not the Secret ID)",
        "         -> client_secret",
        "  7. API permissions should already list Microsoft Graph delegated",
        "     openid, email, profile. Add them if they are missing.",
        "",
        "  A university tenant often needs an admin to consent to a new app.",
        "  If sign-in fails with a consent error, that is why: send the app",
        "  name and client ID to whoever administers LeTourneau 365.",
        "",
        "-- GOOGLE ----------------------------------------------------------",
        "",
        f"  1. {GOOGLE_CONSOLE}  -> create or pick a project",
        "  2. APIs & Services -> OAuth consent screen.",
        "     Internal if the team is on a Google Workspace domain (that",
        "     restricts sign-in to that domain by itself); otherwise External,",
        "     and add each teammate as a test user.",
        "     Scopes: openid, email, profile",
        "  3. Credentials -> Create credentials -> OAuth client ID -> Web",
        "  4. Authorized redirect URIs: add the line above",
        "  5. Copy the Client ID and Client secret into .waam-auth.json",
        "",
        "-- ANYTHING ELSE ---------------------------------------------------",
        "",
        "  Okta, ADFS, Shibboleth: `--template-oidc`, and see",
        "  docs/sso-setup.md, 'Changing which login sits behind the button'.",
        "",
        "-- THEN ------------------------------------------------------------",
        "",
        "  python -m waam_pipeline.authcheck",
        "",
        "  It says PASS or FAIL and prints the one link to hand out. The",
        "  server re-reads the file by itself, so nothing needs restarting.",
        "",
        "  The client secret goes in that gitignored file and NOWHERE else:",
        "  not in a commit, not in a chat message, not in a ticket.",
    ]


def report(path: Path | None = None) -> tuple[bool, list[str]]:

    f = path or oidc._auth_config_file()
    out: list[str] = [f"config       {f}"]

    if not f.exists():
        out += [
            "",
            "FAIL: the file does not exist, so sign-in is OFF and /auth/login",
            "      returns 404. The workbench still runs on ?k= token links.",
            "",
            "      To set it up, in this order:",
            "        python -m waam_pipeline.authcheck --setup      <- start here",
            "        python -m waam_pipeline.authcheck --template > "
            + str(f.name),
            "        python -m waam_pipeline.authcheck",
        ]
        return False, out

    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, out + ["", f"FAIL: unreadable ({exc.__class__.__name__}: {exc})"]

    cfg = oidc.load_auth_config(f)
    if cfg is None:
        return False, out + [
            "",
            "FAIL: the loader refused this file. Its reason is printed above",
            "      this line (load_auth_config writes it to stdout), and the",
            "      server prints the same thing at startup.",
        ]

    problems: list[str] = []
    warnings: list[str] = []


    def _placeholders(obj, prefix=""):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not str(k).startswith("_"):
                    found += _placeholders(v, f"{prefix}{k}.")
        elif isinstance(obj, str) and "FILL-IN" in obj.upper():
            found.append(prefix.rstrip("."))
        return found

    still_blank = _placeholders(raw)
    if still_blank:
        problems.append(
            "still has placeholder values in: " + ", ".join(sorted(still_blank))
            + ". Replace them with the real values before anyone uses this.")

    out += [
        f"provider     {cfg.provider}",
        f"button says  {('Sign in' if cfg.local else 'Sign in with ' + cfg.label)}",
        f"public_url   {cfg.public_url}",
        "",
        "GIVE PEOPLE THIS LINK (one address, the same for everybody):",
        f"    {cfg.public_url.rstrip('/')}/signin",
        "",
    ]
    if cfg.local:


        from . import accounts as _accounts

        store = _accounts.Store()
        n = len(store.emails()) if store.path.exists() else 0
        out += [
            "ACCOUNTS ARE KEPT HERE, not at a provider.",
            f"    records   {store.path.name} ({n} account(s))",
            f"    passwords scrypt n={_accounts.SCRYPT_N}, per-account salt, "
            f"min {_accounts.MIN_PASSWORD_LEN} characters",
            "    manage    python -m waam_pipeline.accounts list | reset | delete",
            "",
        ]
        if cfg.verify_email:
            out += [
                f"    email     VERIFIED by a {_accounts.CODE_DIGITS}-digit code "
                f"via {cfg.mail.host}:{cfg.mail.port}",
                f"              sent as {cfg.mail.sender}",
                "    test it   python -m waam_pipeline.mailer --test you@somewhere",
                "",
            ]
            if cfg.allow_domains and not cfg.open_signin:
                out.append("STRONGEST POSTURE: registration is closed to "
                           "everyone except")
                out.append("addresses on the roster, and a verified "
                           f"{cfg.allow_domains[0]} address gets in by itself.")
            elif cfg.allow_domains and cfg.open_signin:
                warnings.append(
                    "open_signin is true, so anyone with ANY working email "
                    "address can get an account (they are flagged UNRECOGNISED "
                    "for review). Now that addresses are verified you can set "
                    'open_signin false and rely on allow_domains '
                    f'{list(cfg.allow_domains)} instead - that closes the door '
                    "to everyone outside those domains without a roster.")
        else:
            warnings.append(
                "no `mail` block, so there is no verification: an address is "
                "only ever SELF-ASSERTED. allow_domains cannot promote a local "
                "account without it, so the roster is the only way anyone is "
                "RECOGNISED. Add mail settings to change that.")
            if cfg.open_signin:
                warnings.append(
                    "open_signin is true and this URL is public, so ANYONE who "
                    "finds it can create an account. Review arrivals with "
                    "`python -m waam_pipeline.whos_on` and revoke with "
                    "`blocked`.")
        if cfg.verify_email:
            out.append("    forgot    self-service, by emailed code "
                       "(/auth/forgot)")
        else:
            warnings.append(
                "with no mailer there is no self-service password reset "
                "either, so a forgotten password is set by you: "
                "`python -m waam_pipeline.accounts reset <email>`.")
    else:
        out += [
            "PASTE THIS INTO THE PROVIDER CONSOLE, character for character:",
            f"    {cfg.redirect_uri}",
            "",
        ]
    if cfg.provider == "oidc":
        out += [
            f"issuer       {cfg.issuer}",
            f"authorize    {cfg.authorize_endpoint}",
            f"token        {cfg.token_endpoint}",
        ]
        if not cfg.sign_in_label:
            warnings.append(
                'no sign_in_label, so the button reads "Sign in with your '
                'account". Set it to whatever the people pressing it call '
                'their login ("your LeTourneau account").')

    if cfg.public_url.startswith("http://"):


        warnings.append(
            "public_url is http://localhost. Sign-in works in a browser ON "
            "this machine and nowhere else; teammates need the https Funnel "
            "URL here instead.")
    if cfg.public_url.endswith("/"):
        warnings.append("public_url has a trailing slash; the redirect URI is "
                        "built without it, so paste the line above, not a "
                        "concatenation of your own.")

    if cfg.provider == "microsoft":
        if cfg.tenant in oidc.MS_MULTI_TENANT:
            warnings.append(
                f"tenant is {cfg.tenant!r}, which pins nothing. Microsoft's "
                "v2.0 ID tokens usually carry no email_verified claim, so an "
                "unpinned tenant means NO Microsoft address is ever treated "
                "as verified -- allow_domains stops admitting anyone and only "
                "the `users` roster works. Put the Directory (tenant) ID here.")
        else:
            out.append(f"tenant       {cfg.tenant} (pinned - Microsoft refuses "
                       "everyone outside it)")

    if not cfg.users and not cfg.allow_domains and cfg.open_signin:
        warnings.append(
            "no users and no allow_domains, with open_signin true: everyone "
            "who signs in gets a workspace and is flagged UNRECOGNISED. That "
            "is a supported posture -- check "
            "`python -m waam_pipeline.whos_on` after handing the link out.")

    for email in cfg.users:
        domain = email.rsplit("@", 1)[1]
        if email in cfg.blocked or domain in cfg.blocked:
            problems.append(f"{email} is in `users` AND blocked. Blocking wins, "
                            "so that roster entry does nothing.")
    if not cfg.open_signin:
        out.append("open_signin  false - only the roster and verified "
                   "allow_domains addresses get in")

    out.append(f"roster       {len(cfg.users)} named, "
               f"{len(cfg.allow_domains)} domain(s), {len(cfg.blocked)} blocked")


    if cfg.approval_required:
        out.append(f"approval     on - {len(cfg.approved)} approved; a new "
                   "sign-up waits until the owner approves it")
        out.append("             manage   python -m waam_pipeline.approve "
                   "[--list] <email>")


        approve_loads, approve_on_path, approve_line = _approve_command_resolves()
        out.append(f"             command  {approve_line}")
        if not approve_loads:
            problems.append(
                "the pending-notification email tells the owner to run "
                "`waam-approve`, but that console script's target does not "
                "import to a callable -- the [project.scripts] entry is broken, "
                "so the emailed command would be dead.")
        if cfg.approval_notify:
            out.append(f"             notify   {cfg.approval_notify}")
            if approve_loads and cfg.mail is not None and not approve_on_path:
                warnings.append(
                    "the owner-notification email advertises `waam-approve`, "
                    "which is declared in pyproject [project.scripts] but is "
                    "not on PATH here. Install it before the first notice goes "
                    "out (`pip install -e .` on the server, or a `waam-approve` "
                    "wrapper like waam-mailer); until then the owner runs "
                    "`python -m waam_pipeline.approve`.")
        if not cfg.approval_notify:
            warnings.append(
                "approval_required is on but approval_notify is empty, so "
                "nobody is emailed when someone is waiting -- you would see a "
                "request only in `python -m waam_pipeline.approve --list`. Set "
                "approval_notify to the owner's address.")
        elif cfg.mail is None:
            warnings.append(
                "approval_required is on and approval_notify is set, but there "
                "is no `mail` block to send from, so the owner is never emailed "
                "when someone is waiting. Add mail settings, or watch "
                "`python -m waam_pipeline.approve --list`.")

    secret_in_repo = f.name in (Path(p).name for p in
                                _tracked_files(f.parent))
    if secret_in_repo:
        problems.append(f"{f.name} is TRACKED BY GIT. It holds the client "
                        "secret. Remove it from the index and rotate the "
                        "secret in the provider console.")

    if problems:
        out += [""] + [f"FAIL: {p}" for p in problems]
    if warnings:
        out += [""] + [f"warn: {w}" for w in warnings]
    if not problems:
        how = ("they make an account with an email and a password"
               if cfg.local else f'the button reads "Sign in with {cfg.label}"')
        out += ["", f"PASS: sign-in is on. Send people the /signin link above; "
                f"{how}."]
    return not problems, out


def _tracked_files(root: Path) -> list[str]:

    import subprocess

    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True, timeout=5, check=True)
    except Exception:
        return []
    return res.stdout.splitlines()


def _approve_command_resolves() -> tuple[bool, bool, str]:


    import importlib
    import shutil

    target = "waam_pipeline.approve:main"
    try:
        mod = importlib.import_module("waam_pipeline.approve")
        target_loads = callable(getattr(mod, "main", None))
    except Exception:
        target_loads = False

    on_path = shutil.which("waam-approve") is not None
    if not on_path:
        try:
            from importlib import metadata as _md
            on_path = any(ep.name == "waam-approve"
                          for ep in _md.entry_points(group="console_scripts"))
        except Exception:
            on_path = False

    if not target_loads:
        return False, on_path, (
            f"waam-approve -> {target} does NOT load: the [project.scripts] "
            "entry point is broken")
    where = ("on PATH here" if on_path else
             "declared in pyproject [project.scripts]; not installed here")
    return True, on_path, f"waam-approve -> {target} (resolves, {where})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", type=Path, default=None,
                    help="check this file instead of .waam-auth.json")
    ap.add_argument("--setup", action="store_true",
                    help="print how to register the application, with this "
                         "cell's redirect URI already filled in")
    ap.add_argument("--template", action="store_true",
                    help="print a config to fill in, and exit")
    ap.add_argument("--template-oidc", action="store_true",
                    help="print the same for a non-Google/Microsoft provider")
    ap.add_argument("--redirect", action="store_true",
                    help="print only the redirect URI, and exit")
    args = ap.parse_args(argv)

    if args.setup:
        print("\n".join(setup_steps(args.file)))
        return 0
    if args.template_oidc:
        print(json.dumps(TEMPLATE_OIDC, indent=2))
        return 0
    if args.template:
        print(json.dumps(TEMPLATE, indent=2))
        return 0

    ok, lines = report(args.file)
    if args.redirect:
        cfg = oidc.load_auth_config(args.file or oidc._auth_config_file())
        if cfg is None:
            print("no usable config, so there is no redirect URI yet",
                  file=sys.stderr)
            return 1
        print(cfg.redirect_uri)
        return 0
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
