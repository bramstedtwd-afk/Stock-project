"""Doctor: one command that checks everything that can go wrong.

    ./start.sh doctor   (Windows: .\\start.bat doctor)

Runs every health check StockSage depends on and reports PASS/WARN/FAIL
with a plain-language fix for anything that isn't right. Safe to run any
time; changes nothing.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def check_python() -> dict:
    v = sys.version_info
    if v >= (3, 10):
        return _check("Python", PASS, f"{v.major}.{v.minor}.{v.micro}")
    return _check(
        "Python", FAIL, f"{v.major}.{v.minor} is too old",
        "Install Python 3.10+ from python.org and re-run ./start.sh",
    )


CORE_DEPENDENCIES = ("pandas", "numpy", "yfinance", "streamlit")


def check_dependencies(required: tuple[str, ...] = CORE_DEPENDENCIES) -> dict:
    missing = []
    for mod in required:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return _check("Dependencies", PASS, "all core packages import")
    return _check(
        "Dependencies", FAIL, f"missing: {', '.join(missing)}",
        "Run ./start.sh once — it installs everything automatically",
    )


def check_brain() -> dict:
    from .db import Database, default_db_path

    path = default_db_path()
    try:
        db = Database()
        db.conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()
        n_sugg = db.conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
        bootstrap = db.get_meta("bootstrap_done")
        db.close()
    except sqlite3.Error as exc:
        return _check(
            "Brain", FAIL, f"cannot open {path}: {exc}",
            "If the file is corrupted, restore a brain export or delete it to start fresh",
        )
    if bootstrap is None:
        return _check(
            "Brain", WARN, f"healthy at {path}, but not yet bootstrapped",
            "Run the first daily cycle (open the app) to learn from two years of history",
        )
    return _check("Brain", PASS, f"{path} — {n_sugg} calls on record")


def check_market_data() -> dict:
    from .data import MarketData

    df = MarketData().history("SPY")
    if df is not None and len(df) > 50:
        return _check("Market data", PASS, f"SPY history: {len(df)} sessions")
    return _check(
        "Market data", FAIL, "could not fetch SPY from Yahoo Finance",
        "Check your internet connection; if you're online, Yahoo may be "
        "rate-limiting — wait a few minutes and retry",
    )


def check_earnings_calendar(tickers: tuple[str, ...] = ("AAPL", "MSFT", "JPM")) -> dict:
    """Can we resolve an earnings date at all?

    This is worth its own check because failure is *silent*: the blackout
    that stops new entries right before a print simply never fires, and the
    published brief shows `earnings_days: null` on every candidate, which
    reads like "no earnings scheduled" rather than "the lookup is broken".
    Several well-known names are tried, since any one of them may genuinely
    have no date scheduled.
    """
    from .data import MarketData

    market = MarketData()
    errors: list[str] = []
    for ticker in tickers:
        try:
            if market._fetch_earnings_date(ticker):  # bypass the cache
                return _check(
                    "Earnings calendar", PASS, f"resolved a date for {ticker}"
                )
        except Exception as exc:
            errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
    detail = "no earnings date resolved for " + ", ".join(tickers)
    if errors:
        detail += f" — {errors[0]}"
    return _check(
        "Earnings calendar", WARN, detail,
        "Suggestions still work, but the earnings blackout is switched off, "
        "so a buy can land right before a print. Usually Yahoo rate-limiting "
        "or a yfinance version change: try '.\\start.bat update', and if it "
        "persists report this line — the lookup is data.MarketData."
        "_fetch_earnings_date",
    )


def check_robinhood() -> dict:
    from .robinhood import RobinhoodClient

    if not RobinhoodClient.credentials_available():
        return _check(
            "Robinhood", WARN, "not linked (optional)",
            "Link it from the dashboard's Portfolio tab for portfolio-aware suggestions",
        )
    client = RobinhoodClient()
    if client.login(trigger="doctor"):
        client.logout()
        return _check("Robinhood", PASS, "credentials work, login OK")
    return _check(
        "Robinhood", FAIL, "credentials are set but login failed",
        "Re-enter your login on the Portfolio tab; if you use app-based 2FA, "
        "include the TOTP secret",
    )


def check_env_file() -> dict:
    from .envfile import ENV_PATH

    if not ENV_PATH.exists():
        return _check(
            "Settings (.env)", WARN, "no .env file yet",
            "Run ./start.sh once to create it",
        )
    mode = ENV_PATH.stat().st_mode & 0o777
    if os.name == "posix" and mode & 0o077:
        return _check(
            "Settings (.env)", WARN, f"permissions {oct(mode)} are wider than needed",
            f"Run: chmod 600 {ENV_PATH}",
        )
    return _check("Settings (.env)", PASS, "present and private")


def check_account_security() -> dict:
    """Standing exposure of the stored broker login (see stocksage/security.py)."""
    from . import security

    findings = security.audit()
    worst = next((f for f in findings if f["level"] != security.OK), None)
    if worst is None:
        return _check("Account security", PASS, "stored login is as locked down as it gets")
    status = FAIL if worst["level"] == security.CRITICAL else WARN
    return _check(
        "Account security", status, worst["detail"],
        (worst["fix"] + " " if worst["fix"] else "")
        + "Full report: .\\start.bat security",
    )


def check_routine_account() -> dict:
    """The published playbook needs an account number that only .env has.

    ROUTINE.md carries a placeholder rather than the owner's account number,
    because the repository is public. If .env does not supply the real value,
    publish-drive uploads a playbook the routine cannot act on — it is built
    to stop and ask rather than guess, so the failure is safe but it blocks
    the next run, silently, until someone notices.
    """
    from .advisor import AGENTIC_PLACEHOLDER, playbook_for_publishing

    account = (os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    if account:
        if AGENTIC_PLACEHOLDER in playbook_for_publishing():
            return _check(
                "Routine account", WARN,
                "the account is set but the playbook still has a placeholder",
                "The playbook and the substitution have drifted apart — report "
                "this line; it is advisor.playbook_for_publishing",
            )
        return _check(
            "Routine account", PASS, f"published briefs name account ••••{account[-4:]}"
        )
    return _check(
        "Routine account", WARN,
        "STOCKSAGE_AGENTIC_ACCOUNT is not set, so the published playbook "
        "cannot name the account your routine trades",
        "Add a line 'STOCKSAGE_AGENTIC_ACCOUNT=<your account number>' to the "
        ".env file in this folder, then run '.\\start.bat publish-drive' "
        "again. Until then the routine will stop and ask which account to "
        "use rather than guessing.",
    )


def business_days_since(last, now) -> int:
    """Weekdays elapsed after `last`'s date, up to and including `now`'s.

    A Monday 3pm publish checked Wednesday morning is 2: Tuesday and
    Wednesday have both started with no publish in them. Weekends do not
    count, because nothing is expected to publish on them.
    """
    from datetime import timedelta

    days, day = 0, last.date()
    while day < now.date():
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def check_publish_freshness(now=None) -> dict:
    """Is the research the routine reads actually being refreshed?

    The job being *scheduled* proves nothing — a laptop that was asleep, a
    signed-out session or an expired sign-in all leave the schedule intact
    while nothing is published. The routine only finds out when it reads a
    stale brief; this lets the desktop find out first.
    """
    from datetime import datetime, timezone

    from .db import Database

    now = now or datetime.now(timezone.utc)
    drive_set_up = bool(os.environ.get("STOCKSAGE_DRIVE_TOKEN")) or Path(
        "~/.stocksage/drive_token.json"
    ).expanduser().exists()
    if not drive_set_up:
        return _check("Publishing", PASS, "not publishing to Drive (optional)")

    try:
        db = Database()
        last_ok = db.get_meta("last_publish_ok")
        last_err = db.get_meta("last_publish_error")
        db.close()
    except Exception:
        return _check("Publishing", PASS, "skipped — brain unreadable (see Brain above)")

    redo = (
        "Run '.\\start.bat publish-drive' by hand and read what it says. If the "
        "computer was asleep or signed out at publish time, the scheduled runs "
        "were skipped — they only run while this Windows account is signed in "
        "and awake."
    )
    if not last_ok:
        return _check(
            "Publishing", WARN, "no successful publish recorded on this computer yet",
            redo,
        )
    try:
        last = datetime.fromisoformat(last_ok)
    except ValueError:
        return _check("Publishing", WARN, "the last-publish record is unreadable", redo)

    behind = business_days_since(last, now)
    reason = ""
    if last_err and "|" in last_err:
        err_at, _, err_msg = last_err.partition("|")
        if err_at > last_ok:
            reason = f" The last attempt failed: {err_msg}"
    if behind >= 2:
        return _check(
            "Publishing", WARN,
            f"the last successful publish was {last.astimezone():%a %d %b %H:%M} — "
            f"{behind} weekdays ago, so your routine is reading a stale brief."
            + reason,
            redo,
        )
    return _check(
        "Publishing", PASS, f"last published {last.astimezone():%a %d %b %H:%M}"
    )


def check_update_channel() -> dict:
    import subprocess

    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return _check("Update channel", PASS, "git remote configured")
    return _check(
        "Update channel", WARN, "not a git checkout — ./start.sh update won't work",
        "Clone the repo with git instead of downloading a zip to enable updates",
    )


def _job_scheduled(launchd_plist, schtask_name, cron_tag) -> bool:
    from . import autopilot as ap

    try:
        if sys.platform == "darwin":
            return launchd_plist.exists()
        if sys.platform.startswith("win"):
            import subprocess

            # The publish job registers one task per run time, so match any
            # task whose name starts with the base name.
            out = subprocess.run(
                ["schtasks", "/Query", "/FO", "LIST"], capture_output=True, text=True
            )
            return schtask_name in (out.stdout or "")
        return cron_tag in ap._current_crontab()
    except Exception:
        return False


def check_autopilot() -> dict:
    """Both halves of autopilot, reported separately.

    Learning and publishing are independent scheduled jobs, and only the
    publish job refreshes the research the trading routine reads. Reporting
    a single "Autopilot: scheduled" hid a 16-day outage where the brain kept
    learning locally while the routine read a stale brief the whole time.
    """
    from . import autopilot as ap

    learns = _job_scheduled(ap.LAUNCHD_PLIST, ap.SCHTASK_NAME, ap.CRON_TAG)
    publishes = _job_scheduled(
        ap.PUBLISH_LAUNCHD_PLIST, ap.PUBLISH_SCHTASK_NAME, ap.PUBLISH_CRON_TAG
    )
    if learns and publishes:
        return _check(
            "Autopilot", PASS, "learns every weekday and publishes research on schedule"
        )
    if learns and not publishes:
        return _check(
            "Autopilot", WARN,
            "learns every weekday, but does NOT publish — the trading routine "
            "will read older and older research",
            "Run '.\\start.bat autopilot publish-drive' so the brief refreshes "
            "automatically. Without it the brief only updates when you run "
            "publish-drive by hand.",
        )
    if publishes and not learns:
        return _check(
            "Autopilot", WARN, "publishes on schedule, but does not learn",
            "Run '.\\start.bat autopilot' so the model grades its calls daily",
        )
    return _check(
        "Autopilot", WARN, "off — learning only happens when you open the app",
        "Run '.\\start.bat autopilot' to learn every weekday, then "
        "'.\\start.bat autopilot publish-drive' to keep the routine's research fresh",
    )


ALL_CHECKS = (
    check_python,
    check_dependencies,
    check_env_file,
    check_brain,
    check_market_data,
    check_earnings_calendar,
    check_robinhood,
    check_account_security,
    check_routine_account,
    check_publish_freshness,
    check_update_channel,
    check_autopilot,
)

_ICON = {PASS: "✓", WARN: "!", FAIL: "✗"}


def run_doctor() -> int:
    # Library log noise (e.g. yfinance retries) would drown the report.
    import logging

    logging.disable(logging.ERROR)
    print("\nStockSage doctor — checking everything that can go wrong\n")
    worst = PASS
    for check in ALL_CHECKS:
        try:
            result = check()
        except Exception as exc:  # a broken check must not hide the others
            result = _check(check.__name__, FAIL, f"check crashed: {exc}")
        icon = _ICON[result["status"]]
        print(f"  [{icon}] {result['name']:<18} {result['detail']}")
        if result["fix"] and result["status"] != PASS:
            print(f"      → {result['fix']}")
        if result["status"] == FAIL or (result["status"] == WARN and worst == PASS):
            worst = result["status"]
    print()
    if worst == PASS:
        print("Everything looks healthy.\n")
        return 0
    if worst == WARN:
        print("Working, with suggestions above.\n")
        return 0
    print("Something needs fixing — see the arrows above.\n")
    return 1


def main() -> int:
    from .envfile import load_env

    load_env()
    return run_doctor()


if __name__ == "__main__":
    raise SystemExit(main())
