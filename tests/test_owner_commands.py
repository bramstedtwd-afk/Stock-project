"""Every command we hand the owner has to work when they paste it.

Two ways this has already failed on their machine:

  * `stocksage publish-drive` shipped inside an error message — but stocksage
    lives in the project virtualenv and is not on PATH, so it died with
    CommandNotFoundException the moment they tried it.
  * `start.bat update` — PowerShell refuses to run a program from the current
    directory without an explicit `.\\` prefix, so every Windows command we
    printed failed with CommandNotFoundException too.

The owner is non-technical: a command that does not run is not a small
cosmetic bug, it is the feature not existing. These tests read the actual
strings the app prints and fail on either mistake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Sources of text the owner reads: the code that prints it, and the docs.
USER_FACING = [
    PROJECT_ROOT / "stocksage" / name
    for name in (
        "cli.py", "doctor.py", "drive_api.py", "security.py", "update.py",
        "advisor.py", "brain.py", "desktop.py", "engine.py", "envfile.py",
    )
] + [
    PROJECT_ROOT / "app.py",
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "ROUTINE.md",
]
# CLAUDE.md is left out on purpose: it is notes to a future session, not
# something the owner pastes from, and its guardrail sections quote both
# broken forms deliberately as the examples of what not to ship.

# A bare start.bat, i.e. not already prefixed with .\ and not part of a
# filesystem path (autopilot and the desktop shortcut register absolute
# paths, which PowerShell runs happily).
BARE_LAUNCHER = re.compile(r"""(?<![\\./'"])\bstart\.bat\b""")

# The venv-only entry point, presented as if it were on PATH.
BARE_ENTRY_POINT = re.compile(r"(?<![\w./\\-])stocksage\s+(?!import|is\b)[a-z][a-z-]+")


def _offending_lines(path: Path, pattern: re.Pattern) -> list[str]:
    if not path.exists():
        return []
    hits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if pattern.search(line):
            hits.append(f"{path.name}:{number}: {line.strip()}")
    return hits


@pytest.mark.parametrize("path", USER_FACING, ids=lambda p: p.name)
def test_no_bare_start_bat_anywhere_the_owner_can_read_it(path):
    hits = _offending_lines(path, BARE_LAUNCHER)
    assert not hits, (
        "PowerShell will not run these — they need a .\\ prefix:\n" + "\n".join(hits)
    )


@pytest.mark.parametrize("path", USER_FACING, ids=lambda p: p.name)
def test_no_command_assumes_stocksage_is_on_path(path):
    hits = [
        h for h in _offending_lines(path, BARE_ENTRY_POINT)
        if "python -m stocksage" not in h and "-m stocksage" not in h
    ]
    assert not hits, (
        "stocksage is not on PATH — route these through the launcher:\n"
        + "\n".join(hits)
    )


def test_the_launchers_document_themselves_runnably():
    """The help header is the first thing a stuck owner reads."""
    bat = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
    assert not BARE_LAUNCHER.search(bat), "start.bat's own help lists uncallable commands"
    assert ".\\start.bat security" in bat
    sh = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
    assert "./start.sh security" in sh


def test_every_documented_launcher_command_actually_exists():
    """A command in the help that the CLI does not know is a dead end."""
    from stocksage.cli import build_parser

    known = set()
    for action in build_parser()._subparsers._group_actions:
        known |= set(action.choices)
    # Handled by the launcher itself rather than the Python CLI.
    launcher_only = {"install", "phone", "web", "update", "doctor", "autopilot"}

    bat = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
    documented = set(re.findall(r"^rem\s+\.\\start\.bat\s+([a-z][a-z-]*)", bat, re.M))
    unknown = documented - known - launcher_only
    assert not unknown, f"start.bat advertises commands the CLI does not have: {unknown}"
