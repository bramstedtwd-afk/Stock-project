"""Autopilot: learn every weekday after the close, even with nothing open.

    python -m stocksage.autopilot on        schedule the daily cycle
    python -m stocksage.autopilot off       remove the schedule
    python -m stocksage.autopilot status    is it scheduled?

Registers the daily learn+scan cycle with the operating system's own
scheduler — launchd on macOS, cron on Linux, Task Scheduler on Windows —
at 17:30 local time, Monday-Friday (after the US market close if you're in
US Eastern; adjust RUN_HOUR/RUN_MINUTE below for other timezones). Output
lands in ~/.stocksage/daily.log.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = Path("~/.stocksage/daily.log").expanduser()
RUN_HOUR = 17
RUN_MINUTE = 30

LAUNCHD_LABEL = "local.stocksage.daily"
LAUNCHD_PLIST = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()
CRON_TAG = "# stocksage-autopilot"
SCHTASK_NAME = "StockSage Daily"


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


# --- platform appliers ----------------------------------------------------------

def _current_crontab() -> str:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(content: str) -> bool:
    try:
        result = subprocess.run(
            ["crontab", "-"], input=content, capture_output=True, text=True
        )
    except FileNotFoundError:
        say("cron is not available on this system — add this line to any scheduler:")
        say(f"  {cron_line()}")
        return False
    if result.returncode != 0:
        say(f"crontab update failed: {result.stderr.strip()}")
        return False
    return True


def turn_on() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.write_text(launchd_plist())
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
        if not _write_crontab("\n".join(existing + [cron_line()]) + "\n"):
            return 1
    say(
        f"Autopilot ON — StockSage will learn and scan every weekday at "
        f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}. Log: {LOG_FILE}"
    )
    say("(The computer must be awake at that time; a sleeping laptop skips the run.)")
    return 0


def turn_off() -> int:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.unlink(missing_ok=True)
    elif sys.platform.startswith("win"):
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", SCHTASK_NAME], capture_output=True
        )
    else:
        remaining = [
            line for line in _current_crontab().splitlines() if CRON_TAG not in line
        ]
        content = "\n".join(remaining)
        if not _write_crontab(content + "\n" if content else ""):
            return 1
    say("Autopilot OFF — daily runs are back to manual.")
    return 0


def status() -> int:
    if sys.platform == "darwin":
        on = LAUNCHD_PLIST.exists()
    elif sys.platform.startswith("win"):
        on = (
            subprocess.run(
                ["schtasks", "/Query", "/TN", SCHTASK_NAME], capture_output=True
            ).returncode
            == 0
        )
    else:
        on = CRON_TAG in _current_crontab()
    say(
        f"Autopilot is {'ON — weekdays at %02d:%02d' % (RUN_HOUR, RUN_MINUTE) if on else 'OFF'}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = args[0] if args else "on"
    if mode in ("on", "install"):
        return turn_on()
    if mode in ("off", "uninstall"):
        return turn_off()
    if mode == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
