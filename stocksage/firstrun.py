"""First run on a new machine: get from a fresh clone to a working install.

Moving machines used to be six separate steps with three file paths to type
correctly — download the brain, import it by full path, edit .env in
Notepad, link the broker, check it, schedule it. Every one of those is a
place for a non-technical owner to stall, and the failures are quiet: an
unimported brain just looks like a tool that has learned nothing.

This module is the guided version. It works out what is missing, finds the
brain file itself in the usual download folders, writes the one setting
that has to be in .env, and then hands off to doctor.

It never asks for broker credentials. Those are typed into the dashboard's
Portfolio tab by the owner and stored only in .env — a terminal prompt is
the wrong place to teach anyone to type a brokerage password.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BRAIN_FILENAMES = ("brain-snapshot.db", "stocksage.db", "stocksage-brain.db")


@dataclass
class Step:
    """One thing still to do, in the owner's words."""

    key: str
    done: bool
    detail: str


def find_brain_files(roots: list[Path] | None = None) -> list[Path]:
    """Brain files sitting where a browser download or a USB stick puts them.

    Newest first, because someone who exported twice wants the recent one.
    Anything unreadable as SQLite is skipped rather than offered — importing
    a half-downloaded file is a worse outcome than not finding one.
    """
    import sqlite3

    if roots is None:
        home = Path.home()
        roots = [home / "Downloads", home / "Desktop", home]
        roots += [Path(f"{d}:/") for d in "DEFG"]

    found: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for name in BRAIN_FILENAMES:
                candidate = root / name
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
        except OSError:
            continue  # an unreadable drive letter is not an error here

    readable = []
    for path in found:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.execute("SELECT 1 FROM suggestions LIMIT 1").fetchone()
            conn.close()
            readable.append(path)
        except Exception:
            continue
    readable.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return readable


def brain_is_populated(db_path: str | Path | None = None) -> bool:
    """True when this machine's brain already knows something."""
    from .brain import brain_info

    try:
        info = brain_info(db_path)
    except Exception:
        return False
    return bool(info["suggestions"] or info["move_events"])


def remaining_steps() -> list[Step]:
    """What still stands between this machine and a working install."""
    import os

    from .robinhood import RobinhoodClient

    account = (os.environ.get("STOCKSAGE_AGENTIC_ACCOUNT") or "").strip()
    populated = brain_is_populated()
    linked = RobinhoodClient.credentials_available()
    drive = (Path("~/.stocksage/drive_token.json").expanduser().exists()
             or Path("~/.stocksage/drive_credentials.json").expanduser().exists())

    return [
        Step("brain", populated,
             "carries what it learned on your old machine"
             if populated else "has learned nothing yet"),
        Step("robinhood", linked,
             "linked" if linked else "not linked — no portfolio-aware suggestions"),
        Step("account", bool(account),
             f"routine trades account ••••{account[-4:]}" if account
             else "the published playbook cannot name the account to trade"),
        Step("drive", drive,
             "set up" if drive else "not set up — only needed for the trading routine"),
    ]


def set_agentic_account(account: str) -> str:
    """Store the routine's account number in .env. Returns what was stored."""
    from .envfile import save_env

    account = "".join(ch for ch in account if ch.isdigit())
    if not account:
        raise ValueError("that doesn't look like an account number")
    save_env({"STOCKSAGE_AGENTIC_ACCOUNT": account})
    return account


def import_brain_file(path: str | Path) -> dict:
    """Merge a brain file into this machine's brain."""
    from .brain import import_brain

    return import_brain(path)
