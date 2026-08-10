"""Autopilot: learn and publish research on a schedule, hands-off.

    python -m stocksage.autopilot on              schedule the daily learn cycle
    python -m stocksage.autopilot publish DIR     publish to a Drive-synced folder
    python -m stocksage.autopilot publish-drive   publish via Drive's API directly
                                                   (no desktop app, no admin rights —
                                                   for locked-down/managed machines)
    python -m stocksage.autopilot off             remove all schedules
    python -m stocksage.autopilot status          what's scheduled?

Registers jobs with the operating system's own scheduler — launchd on macOS,
cron on Linux, Task Scheduler on Windows — Monday-Friday, at whatever local
time your computer's own clock reads. There is no timezone conversion
anywhere in this module: if your machine's clock says 08:30, the job fires
at 08:30 in whatever zone that clock is set to. Set RUN_HOUR/RUN_MINUTE and
PUBLISH_TIMES to the local times you actually want.

- The **daily** job (17:30 local) runs the learn+scan cycle.
- The **publish** job (default 08:30, 09:45, 11:15, 12:45, 14:30 local — set
  STOCKSAGE_PUBLISH_TIMES to override) runs the full capture->grade->supply
  cycle: it mirrors your Robinhood fills into graded calls, grades matured
  ones, and writes a fresh research brief into your Google-Drive-synced
  folder so the trading routine reads freshly-graded research every run.
  Defaults are timed to land shortly before a routine firing every ~90min
  starting mid-morning; adjust to match your actual routine schedule.

Output lands in ~/.stocksage/daily.log and ~/.stocksage/publish.log.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = Path("~/.stocksage/daily.log").expanduser()
PUBLISH_LOG = Path("~/.stocksage/publish.log").expanduser()
RUN_HOUR = 17
RUN_MINUTE = 30


def _parse_times(value: str | None, default: tuple[tuple[int, int], ...]):
    """Parse 'HH:MM,HH:MM' into ((h,m),...); fall back to default on junk."""
    if not value:
        return default
    out = []
    for tok in value.split(","):
        tok = tok.strip()
        if ":" not in tok:
            continue
        h, _, m = tok.partition(":")
        try:
            out.append((int(h), int(m)))
        except ValueError:
            continue
    return tuple(out) or default


# Publish just BEFORE each routine run so every run reads fresh, freshly-graded
# research. Defaults cover routine runs at 08:45, 10:00, 11:30, 13:00, 14:45.
# Override with STOCKSAGE_PUBLISH_TIMES="08:30,09:45,11:15,12:45,14:30".
PUBLISH_TIMES = _parse_times(
    os.environ.get("STOCKSAGE_PUBLISH_TIMES"),
    ((8, 30), (9, 45), (11, 15), (12, 45), (14, 30)),
)

LAUNCHD_LABEL = "local.stocksage.daily"
LAUNCHD_PLIST = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()
CRON_TAG = "# stocksage-autopilot"
SCHTASK_NAME = "StockSage Daily"

PUBLISH_LAUNCHD_LABEL = "local.stocksage.publish"
PUBLISH_LAUNCHD_PLIST = Path(
    f"~/Library/LaunchAgents/{PUBLISH_LAUNCHD_LABEL}.plist"
).expanduser()
PUBLISH_CRON_TAG = "# stocksage-publish"
PUBLISH_SCHTASK_NAME = "StockSage Publish"


def say(msg: str) -> None:
    print(f"[stocksage] {msg}")


# --- pure content builders (unit-tested) ---------------------------------------

def cron_line() -> str:
    return (
        f"{RUN_MINUTE} {RUN_HOUR} * * 1-5 "
        f'"{PROJECT_ROOT}/start.sh" daily >> "{LOG_FILE}" 2>&1 {CRON_TAG}'
    )


def launchd_plist() -> str:
    intervals = "\n".join(
        "    <dict>"
        f"<key>Weekday</key><integer>{d}</integer>"
        f"<key>Hour</key><integer>{RUN_HOUR}</integer>"
        f"<key>Minute</key><integer>{RUN_MINUTE}</integer>"
        "</dict>"
        for d in range(1, 6)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{PROJECT_ROOT}/start.sh</string>
    <string>daily</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>
  <key>StandardOutPath</key><string>{LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{LOG_FILE}</string>
</dict></plist>
"""


def schtasks_create_cmd() -> list[str]:
    return [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        SCHTASK_NAME,
        "/TR",
        f'"{PROJECT_ROOT / "start.bat"}" daily',
        "/SC",
        "WEEKLY",
        "/D",
        "MON,TUE,WED,THU,FRI",
        "/ST",
        f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}",
    ]


# --- publish job content builders (unit-tested) ---------------------------------

def _publish_subcommand(folder: str | None) -> str:
    """'publish "<folder>"' for the synced-folder path, or 'publish-drive'
    for the no-install, direct-API path when folder is None."""
    return f'publish "{folder}"' if folder else "publish-drive"


def publish_cron_lines(folder: str | None = None) -> list[str]:
    sub = _publish_subcommand(folder)
    return [
        f'{m} {h} * * 1-5 "{PROJECT_ROOT}/start.sh" {sub}'
        f' >> "{PUBLISH_LOG}" 2>&1 {PUBLISH_CRON_TAG}'
        for h, m in PUBLISH_TIMES
    ]


def publish_launchd_plist(folder: str | None = None) -> str:
    intervals = "\n".join(
        "    <dict>"
        f"<key>Weekday</key><integer>{d}</integer>"
        f"<key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>{m}</integer>"
        "</dict>"
        for h, m in PUBLISH_TIMES
        for d in range(1, 6)
    )
    args = (
        f"    <string>publish</string>\n    <string>{folder}</string>"
        if folder
        else "    <string>publish-drive</string>"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{PUBLISH_LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{PROJECT_ROOT}/start.sh</string>
{args}
  </array>
  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>
  <key>StandardOutPath</key><string>{PUBLISH_LOG}</string>
  <key>StandardErrorPath</key><string>{PUBLISH_LOG}</string>
</dict></plist>
"""


def publish_schtasks_cmds(folder: str | None = None) -> list[list[str]]:
    sub = _publish_subcommand(folder)
    return [
        [
            "schtasks", "/Create", "/F",
            "/TN", f"{PUBLISH_SCHTASK_NAME} {h:02d}{m:02d}",
            "/TR", f'"{PROJECT_ROOT / "start.bat"}" {sub}',
            "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI",
            "/ST", f"{h:02d}:{m:02d}",
        ]
        for h, m in PUBLISH_TIMES
    ]


# --- platform appliers ----------------------------------------------------------

def _current_crontab() -> str:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(content: str, fallback_lines: list[str] | None = None) -> bool:
    try:
        result = subprocess.run(
            ["crontab", "-"], input=content, capture_output=True, text=True
        )
    except FileNotFoundError:
        if fallback_lines:
            say("cron is not available on this system — add to any scheduler:")
            for line in fallback_lines:
                say(f"  {line}")
        return False
    if result.returncode != 0:
        say(f"crontab update failed: {result.stderr.strip()}")
        return False
    return True


def turn_on() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.write_text(launchd_plist(), encoding="utf-8")
        subprocess.run(
            ["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True
        )  # re-load cleanly if it already existed
        result = subprocess.run(
            ["launchctl", "load", str(LAUNCHD_PLIST)], capture_output=True, text=True
        )
        if result.returncode != 0:
            say(f"launchctl load failed: {result.stderr.strip()}")
            return 1
    elif sys.platform.startswith("win"):
        result = subprocess.run(schtasks_create_cmd(), capture_output=True, text=True)
        if result.returncode != 0:
            say(f"Task Scheduler registration failed: {result.stderr.strip()}")
            return 1
    else:
        existing = [
            line for line in _current_crontab().splitlines() if CRON_TAG not in line
        ]
        if not _write_crontab(
            "\n".join(existing + [cron_line()]) + "\n", fallback_lines=[cron_line()]
        ):
            return 1
    say(
        f"Autopilot ON — StockSage will learn and scan every weekday at "
        f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}. Log: {LOG_FILE}"
    )
    say("(The computer must be awake at that time; a sleeping laptop skips the run.)")
    return 0


def turn_on_publish(folder: str | None) -> int:
    """Schedule the publish job. `folder` is a Drive-synced directory for
    the local-folder path; pass None for the direct-API path (no desktop
    app, no folder needed — see stocksage/drive_api.py)."""
    if folder:
        folder_path = Path(folder).expanduser()
        if not folder_path.parent.exists():
            say(f"Parent of {folder_path} doesn't exist — create the folder first.")
            return 1
        folder_path.mkdir(parents=True, exist_ok=True)
        folder = str(folder_path)
    else:
        from .drive_api import credentials_available

        if not credentials_available():
            say(
                "Google Drive API isn't set up yet — see the README for the "
                "one-time, no-install browser sign-in steps, then retry."
            )
            return 1
    PUBLISH_LOG.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        PUBLISH_LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        PUBLISH_LAUNCHD_PLIST.write_text(publish_launchd_plist(folder), encoding="utf-8")
        subprocess.run(
            ["launchctl", "unload", str(PUBLISH_LAUNCHD_PLIST)], capture_output=True
        )
        result = subprocess.run(
            ["launchctl", "load", str(PUBLISH_LAUNCHD_PLIST)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            say(f"launchctl load failed: {result.stderr.strip()}")
            return 1
    elif sys.platform.startswith("win"):
        for cmd in publish_schtasks_cmds(folder):
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                say(f"Task Scheduler registration failed: {result.stderr.strip()}")
                return 1
    else:
        existing = [
            line for line in _current_crontab().splitlines()
            if PUBLISH_CRON_TAG not in line
        ]
        lines = publish_cron_lines(folder)
        if not _write_crontab(
            "\n".join(existing + lines) + "\n", fallback_lines=lines
        ):
            return 1
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in PUBLISH_TIMES)
    if folder:
        say(f"Publish autopilot ON — research goes to {folder} every weekday at {times}.")
        say("Make sure that folder is synced by Google Drive for Desktop so the routine sees it.")
    else:
        say(f"Publish autopilot ON — research goes straight to Google Drive's API "
            f"every weekday at {times}. No desktop app, no folder syncing.")
    return 0


def _remove_publish_jobs() -> None:
    if sys.platform == "darwin":
        subprocess.run(
            ["launchctl", "unload", str(PUBLISH_LAUNCHD_PLIST)], capture_output=True
        )
        PUBLISH_LAUNCHD_PLIST.unlink(missing_ok=True)
    elif sys.platform.startswith("win"):
        for h, m in PUBLISH_TIMES:
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", f"{PUBLISH_SCHTASK_NAME} {h:02d}{m:02d}"],
                capture_output=True,
            )


def turn_off() -> int:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.unlink(missing_ok=True)
        _remove_publish_jobs()
    elif sys.platform.startswith("win"):
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", SCHTASK_NAME], capture_output=True
        )
        _remove_publish_jobs()
    else:
        remaining = [
            line for line in _current_crontab().splitlines()
            if CRON_TAG not in line and PUBLISH_CRON_TAG not in line
        ]
        content = "\n".join(remaining)
        if not _write_crontab(content + "\n" if content else ""):
            return 1
    say("Autopilot OFF — daily learning and research publishing are back to manual.")
    return 0


def _publish_on() -> bool:
    if sys.platform == "darwin":
        return PUBLISH_LAUNCHD_PLIST.exists()
    if sys.platform.startswith("win"):
        h, m = PUBLISH_TIMES[0]
        return subprocess.run(
            ["schtasks", "/Query", "/TN", f"{PUBLISH_SCHTASK_NAME} {h:02d}{m:02d}"],
            capture_output=True,
        ).returncode == 0
    return PUBLISH_CRON_TAG in _current_crontab()


def status() -> int:
    if sys.platform == "darwin":
        daily_on = LAUNCHD_PLIST.exists()
    elif sys.platform.startswith("win"):
        daily_on = subprocess.run(
            ["schtasks", "/Query", "/TN", SCHTASK_NAME], capture_output=True
        ).returncode == 0
    else:
        daily_on = CRON_TAG in _current_crontab()
    say(
        "Daily learning: "
        + (f"ON — weekdays {RUN_HOUR:02d}:{RUN_MINUTE:02d}" if daily_on else "OFF")
    )
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in PUBLISH_TIMES)
    say("Research publishing: " + (f"ON — weekdays {times}" if _publish_on() else "OFF"))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = args[0] if args else "on"
    if mode in ("on", "install"):
        return turn_on()
    if mode == "publish":
        if len(args) < 2:
            say("Usage: autopilot publish <drive-synced-folder>")
            say("(No folder to sync? Use: autopilot publish-drive — no install needed)")
            return 2
        return turn_on_publish(args[1])
    if mode == "publish-drive":
        return turn_on_publish(None)
    if mode in ("off", "uninstall"):
        return turn_off()
    if mode == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
