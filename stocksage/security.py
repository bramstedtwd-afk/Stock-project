"""Account security: who touched the broker, when, and how exposed the keys are.

Two jobs, both aimed at one question the owner actually asks — *"Robinhood
just emailed me about a sign-in. Was that me, my app, or somebody else?"*

    access log   every broker session this app opens is written to a local
                 JSONL file with a UTC timestamp, the command that caused
                 it, and whether it reused the cached session or performed a
                 **fresh sign-in** (the kind Robinhood notifies you about).
                 Match a Robinhood alert against this list and the answer is
                 immediate instead of a guess.

    audit        the standing exposure checks: file permissions on .env and
                 on the broker token, and whether the TOTP seed is sitting
                 in the same file as the password (which silently turns
                 two-factor back into one-factor).

The log lives beside the brain in ~/.stocksage and is deliberately NOT part
of the brain: brains get exported, merged, committed as a repo snapshot and
uploaded to Drive. Sign-in history for a real brokerage account should never
ride along with any of that. No credential, token, or account number is ever
written here — only timestamps and event names.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path("~/.stocksage").expanduser()
ACCESS_LOG = STATE_DIR / "access.log"

# Keep the log small enough to read by eye; a year of normal use is ~400 lines.
MAX_ACCESS_LINES = 2000

# Event kinds written to the log.
FRESH_LOGIN = "fresh-login"      # full username/password auth — Robinhood alerts on this
SESSION_REUSED = "session-reused"  # cached token still valid — no alert, no new device
LOGIN_FAILED = "login-failed"


def access_log_path() -> Path:
    """Resolved at call time so tests can redirect it via STOCKSAGE_STATE."""
    override = os.environ.get("STOCKSAGE_STATE")
    return (Path(override).expanduser() / "access.log") if override else ACCESS_LOG


def token_pickle_path() -> Path:
    """Where robin_stocks caches the session token (its own fixed location)."""
    return Path("~/.tokens/robinhood.pickle").expanduser()


def harden_file(path: Path) -> bool:
    """Make a file readable only by its owner. True when it took effect.

    POSIX gets a plain chmod. Windows ignores chmod's group/other bits
    entirely, so the equivalent there is an explicit ACL: break inheritance
    from the parent folder and grant full control to this user alone.
    """
    path = Path(path)
    if not path.exists():
        return False
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            if not user:
                return False
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, text=True, timeout=20, check=True,
            )
            return True
        path.chmod(0o600)
        return True
    except Exception:
        # Hardening is best-effort: a locked-down file is better, but failing
        # to lock it down must never stop the app from running.
        return False


def record_access(event: str, trigger: str = "unknown", detail: str = "") -> None:
    """Append one broker-access event. Never raises — this is bookkeeping."""
    try:
        path = access_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                "trigger": trigger,
                "detail": detail,
                "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
            }
        )
        with path.open("a", encoding="utf-8") as fh:
            # A run killed mid-write (the machine sleeping through a scheduled
            # publish) leaves a line with no terminator. Appending straight
            # onto it would fuse the two and lose the new event as well as
            # the old, so start a fresh line first.
            if path.stat().st_size and not _ends_with_newline(path):
                fh.write("\n")
            fh.write(line + "\n")
        harden_file(path)
        _trim(path)
    except Exception:
        pass


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def _trim(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_ACCESS_LINES:
        path.write_text(
            "\n".join(lines[-MAX_ACCESS_LINES:]) + "\n", encoding="utf-8"
        )


def recent_access(limit: int = 20, since_hours: float | None = None) -> list[dict]:
    """Most recent broker-access events, newest first."""
    path = access_log_path()
    if not path.exists():
        return []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
        if since_hours is not None
        else None
    )
    events: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # a torn line from an interrupted write is not fatal
        if cutoff is not None:
            try:
                if datetime.fromisoformat(entry["at"]) < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
        events.append(entry)
    events.reverse()
    return events[:limit]


def explain_signin(when_iso: str, tolerance_minutes: int = 10) -> dict:
    """Was a Robinhood sign-in alert at this time caused by this app?

    Returns {"ours": bool, "match": entry|None, "note": str}. Only a
    *fresh* login produces a Robinhood alert, so a nearby session-reuse
    entry is reported as evidence the alert was NOT ours.
    """
    try:
        target = datetime.fromisoformat(when_iso)
    except ValueError:
        return {"ours": False, "match": None, "note": f"could not read the time {when_iso!r}"}
    if target.tzinfo is None:
        target = target.astimezone()
    window = timedelta(minutes=tolerance_minutes)
    nearest, nearest_gap = None, None
    for entry in recent_access(limit=MAX_ACCESS_LINES):
        try:
            at = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            continue
        gap = abs(at - target)
        if nearest_gap is None or gap < nearest_gap:
            nearest, nearest_gap = entry, gap
        if gap <= window and entry.get("event") == FRESH_LOGIN:
            return {
                "ours": True,
                "match": entry,
                "note": (
                    f"StockSage signed in at {entry['at']} (UTC) from "
                    f"{entry.get('trigger', 'unknown')} — that is this alert."
                ),
            }
    if nearest is None:
        return {
            "ours": False, "match": None,
            "note": (
                "StockSage has no record of opening a broker session at all. "
                "Treat this sign-in as somebody else until you have checked "
                "Robinhood's own device list."
            ),
        }
    return {
        "ours": False, "match": nearest,
        "note": (
            f"No StockSage sign-in within {tolerance_minutes} minutes. The closest "
            f"thing it did was '{nearest.get('event')}' at {nearest['at']} (UTC). "
            "A reused session does not trigger a Robinhood alert, so this sign-in "
            "was not StockSage."
        ),
    }


# --- standing exposure audit ------------------------------------------------

OK, RISK, CRITICAL = "ok", "risk", "critical"


def _finding(name: str, level: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "level": level, "detail": detail, "fix": fix}


def audit() -> list[dict]:
    """Everything about the account link that is worth knowing, worst first."""
    from .envfile import ENV_PATH

    findings: list[dict] = []

    if not ENV_PATH.exists():
        findings.append(_finding("Settings file", OK, "no .env — nothing stored yet"))
    else:
        mode = ENV_PATH.stat().st_mode & 0o777
        if os.name == "posix" and mode & 0o077:
            findings.append(
                _finding(
                    "Settings file", RISK,
                    f".env is readable by other accounts on this machine ({oct(mode)})",
                    f"chmod 600 {ENV_PATH}",
                )
            )
        else:
            findings.append(_finding("Settings file", OK, ".env is private to you"))

        body = ENV_PATH.read_text(encoding="utf-8", errors="replace")
        has_password = "ROBINHOOD_PASSWORD=" in body
        has_secret = any(
            line.strip().startswith("ROBINHOOD_MFA_SECRET=")
            and line.split("=", 1)[1].strip()
            for line in body.splitlines()
        )
        if has_password and has_secret:
            findings.append(
                _finding(
                    "Two-factor", CRITICAL,
                    "your password AND your two-factor seed are in the same file, "
                    "so anything that reads .env can generate valid 2FA codes — "
                    "the second factor is not adding protection",
                    "Decide deliberately: keep it (unattended runs work, one file "
                    "protects everything) or remove ROBINHOOD_MFA_SECRET and sign "
                    "in by hand. See 'start.bat security' for the trade-off.",
                )
            )
        elif has_secret:
            findings.append(
                _finding("Two-factor", OK, "TOTP seed stored without a password beside it")
            )
        else:
            findings.append(
                _finding("Two-factor", OK, "no TOTP seed stored on this machine")
            )

    pickle = token_pickle_path()
    if not pickle.exists():
        findings.append(
            _finding(
                "Session token", OK,
                "no cached broker token — every run signs in fresh",
            )
        )
    else:
        mode = pickle.stat().st_mode & 0o777
        if os.name == "posix" and mode & 0o077:
            findings.append(
                _finding(
                    "Session token", RISK,
                    f"the cached broker token is readable by others ({oct(mode)}); "
                    "it is a bearer key — holding it is as good as being logged in",
                    f"chmod 600 {pickle}",
                )
            )
        else:
            findings.append(_finding("Session token", OK, "cached token is private to you"))

    fresh = [e for e in recent_access(limit=MAX_ACCESS_LINES, since_hours=24 * 7)
             if e.get("event") == FRESH_LOGIN]
    per_day = len(fresh) / 7.0
    if per_day > 2:
        findings.append(
            _finding(
                "Sign-in noise", RISK,
                f"{len(fresh)} fresh sign-ins in the last 7 days (~{per_day:.1f}/day) — "
                "so many alerts that a real intrusion would blend in",
                "Let one scheduled run touch the broker and make the rest "
                "publish-only (--no-broker).",
            )
        )
    else:
        findings.append(
            _finding(
                "Sign-in noise", OK,
                f"{len(fresh)} fresh sign-ins in the last 7 days — "
                "few enough that an unexpected alert stands out",
            )
        )

    order = {CRITICAL: 0, RISK: 1, OK: 2}
    findings.sort(key=lambda f: order.get(f["level"], 3))
    return findings
