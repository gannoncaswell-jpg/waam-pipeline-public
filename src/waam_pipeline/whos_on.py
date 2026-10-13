"Who has been on the workbench, from the server's own access log."


from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

from .server import ACCESS_LOG


def _rows(path: Path, since: dt.datetime):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            when = dt.datetime.fromisoformat(row["t"])
        except (ValueError, KeyError):
            continue
        if when >= since:
            row["_when"] = when
            out.append(row)
    return out


ACTIVITY = {
    "register-ok": "account created",
    "verify-ok": "email verified",
    "code-sent": "verification code sent",
    "reset-sent": "password-reset code sent",
    "signin-ok": "signed in",
    "guest-start": "looked as a guest",
}


ALARMS = {
    "REFUSED-ratelimit": ("guessing cut off",
                          "somebody was guessing a password or a code. "
                          "Rotating links does NOT help; check whose address "
                          "it is, and block it if it is not one of yours."),
    "REFUSED-code": ("wrong verification code",
                     "a few is a mistyped code; a run of them is not."),
    "REFUSED-mail": ("the sender FAILED",
                     "nobody can register or reset while this is happening. "
                     "Run `python -m waam_pipeline.mailer --test`."),
    "REFUSED-blocked": ("a blocked account tried to return",
                        "the block is working. Nothing to do."),
    "REFUSED-unknown": ("an address not admitted by the rules tried to join",
                        "expected while registration is closed."),
}


def report(hours: float = 24.0, path: Path | None = None) -> str:
    path = path or ACCESS_LOG
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    rows = _rows(path, since)
    if not rows:
        return (f"no recorded arrivals in the last {hours:g} h\n"
                f"  (log: {path})")

    lines = [f"workbench arrivals, last {hours:g} h  ({len(rows)} requests)", ""]
    seen: dict = defaultdict(lambda: {"n": 0, "first": None, "last": None,
                                      "via": set(), "agents": set(),
                                      "emails": set()})


    accounts: dict = defaultdict(lambda: {"n": 0, "last": None, "known": True,
                                          "workspaces": set()})
    for r in rows:
        key = (r.get("workspace") or "-REFUSED-", r.get("ip") or "?")
        e = seen[key]
        e["n"] += 1
        e["first"] = min(e["first"] or r["_when"], r["_when"])
        e["last"] = max(e["last"] or r["_when"], r["_when"])
        if r.get("via"):
            e["via"].add(r["via"])
        if r.get("agent"):
            e["agents"].add(r["agent"][:60])
        email = r.get("email")
        if email:
            e["emails"].add(email)


        if email and not str(r.get("outcome") or "").startswith("REFUSED"):
            a = accounts[email]
            a["n"] += 1
            a["last"] = max(a["last"] or r["_when"], r["_when"])
            if r.get("known") is False:
                a["known"] = False
            if r.get("workspace"):
                a["workspaces"].add(r["workspace"])

    lines.append(f"  {'workspace':<22} {'address':<22} {'reqs':>5}  last seen")
    for (ws, ip), e in sorted(seen.items(), key=lambda kv: kv[1]["last"], reverse=True):


        shown = ws if len(ws) <= 22 else ws[:21] + "~"
        if ws.startswith("guest-"):
            shown = "(guest) " + shown[6:]
        lines.append(f"  {shown:<22} {ip:<22} {e['n']:>5}  "
                     f"{e['last'].astimezone().strftime('%H:%M:%S')}")
        if e["emails"]:
            how = "refused as" if ws == "-REFUSED-" else "signed in as"
            lines.append(f"  {'':<22} {how} {', '.join(sorted(e['emails']))}")
        if e["via"]:
            lines.append(f"  {'':<22} via proxy {', '.join(sorted(e['via']))}")
        for agent in sorted(e["agents"]):
            lines.append(f"  {'':<22}   {agent}")

    if accounts:
        lines.append("")
        lines.append("  accounts")
        for email, a in sorted(accounts.items(), key=lambda kv: kv[1]["last"],
                               reverse=True):
            flag = "  " if a["known"] else "!!"
            ws = ", ".join(sorted(a["workspaces"])) or "-"
            lines.append(f"  {flag} {email:<34} {a['n']:>5} reqs  "
                         f"{a['last'].astimezone().strftime('%H:%M:%S')}  -> {ws}")

    unknown = sorted(e for e, a in accounts.items() if not a["known"])
    lines.append("")
    if unknown:
        lines.append(f"  !! {len(unknown)} UNRECOGNISED account(s) signed in: "
                     f"{', '.join(unknown)}")
        lines.append("     Sign-in admits anyone who reaches the URL and labels "
                     "them; recognising them is")
        lines.append("     this report's job. If you do not know one, add it to "
                     '"blocked" in .waam-auth.json')
        lines.append("     - it takes effect on the next request and ends the "
                     "session it already holds.")
    elif accounts:
        lines.append("  every signed-in account is one you have named")


    counts: dict = defaultdict(int)
    for r in rows:
        out = str(r.get("outcome") or "")
        if out in ACTIVITY:
            counts[out] += 1
        elif out.startswith("reset-ok"):
            counts["reset-ok"] += 1
    if counts:
        lines.append("")
        lines.append("  sign-in activity")
        for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            what = ACTIVITY.get(key, "password reset")
            lines.append(f"     {n:>4}  {what}")
        if counts.get("reset-ok"):
            resets = sorted({r["email"] for r in rows
                             if str(r.get("outcome") or "").startswith("reset-ok")
                             and r.get("email")})
            lines.append(f"     !! a password was reset for: {', '.join(resets)}")
            lines.append("        If that was not them, block the address now - "
                         "a reset also ended")
            lines.append("        every session that account was holding.")

    alarms: dict = defaultdict(list)
    for r in rows:
        out = str(r.get("outcome") or "")
        if out in ALARMS:
            alarms[out].append(r)
    if alarms:
        lines.append("")
        lines.append("  things worth a look")
        for key, hits in sorted(alarms.items(), key=lambda kv: -len(kv[1])):
            what, advice = ALARMS[key]
            who = sorted({h["email"] for h in hits if h.get("email")})
            lines.append(f"     {len(hits):>4}  {what}"
                         + (f"  ({', '.join(who)})" if who else ""))
            lines.append(f"           {advice}")

    refused = [r for r in rows
               if str(r.get("outcome") or "").startswith("REFUSED")
               and str(r.get("outcome")) not in ALARMS]
    if refused:
        addrs = sorted({r.get("ip") or "?" for r in refused})
        emails = sorted({r["email"] for r in refused if r.get("email")})
        lines.append(f"  !! {len(refused)} REFUSED from {len(addrs)} address(es): "
                     f"{', '.join(addrs)}")
        if emails:
            lines.append(f"     refused accounts: {', '.join(emails)}")
        lines.append("     Someone reached the URL with no valid link and no "
                     "session - a probe, a stale")
        lines.append("     link, or a typo. If you cannot account for it, rotate "
                     "the tokens (delete the")
        lines.append("     .waam-tokens.json entries and reissue) and take Funnel "
                     "down.")
    elif not alarms:
        lines.append("  no refused attempts: every arrival carried a valid link "
                     "or a signed-in session")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--log", type=Path, default=None)
    a = ap.parse_args()
    print(report(a.hours, a.log))


if __name__ == "__main__":
    main()
