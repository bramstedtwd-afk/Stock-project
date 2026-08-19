"""This repository is public. None of the owner's data may be in it.

The repo doubles as a portfolio piece, so strangers read it. Everything
personal — the brokerage account number, real holdings and balances, the
owner's name and home directory — belongs in .env or the local brain, never
in a tracked file. These tests are the standing check on that, because the
leak that matters is the one added six months from now in a hurry.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.split()
    keep = {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml",
            ".bat", ".sh", ".example", ".json"}
    return [
        PROJECT_ROOT / f
        for f in out
        if (PROJECT_ROOT / f).suffix in keep and (PROJECT_ROOT / f).is_file()
    ]


TRACKED = _tracked_text_files()


def _scan(pattern: re.Pattern, allow=()) -> list[str]:
    hits = []
    for path in TRACKED:
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(body.splitlines(), 1):
            if pattern.search(line) and not any(a in line for a in allow):
                rel = path.relative_to(PROJECT_ROOT)
                hits.append(f"{rel}:{number}: {line.strip()[:110]}")
    return hits


def test_no_real_brokerage_account_number():
    """The agentic account number is .env configuration. The synthetic
    123456789 / 111111111 / 999999999 used in tests are deliberately fake."""
    synthetic = {"123456789", "111111111", "999999999", "000000000"}
    hits = []
    for path in TRACKED:
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(body.splitlines(), 1):
            for candidate in re.findall(r"(?<![\d.])\d{9}(?![\d.])", line):
                if candidate not in synthetic:
                    rel = path.relative_to(PROJECT_ROOT)
                    hits.append(f"{rel}:{number}: {candidate}")
    assert not hits, "possible real account number in a public file:\n" + "\n".join(hits)


def test_the_playbook_carries_a_placeholder_not_an_account():
    from stocksage.advisor import AGENTIC_PLACEHOLDER

    playbook = (PROJECT_ROOT / "ROUTINE.md").read_text(encoding="utf-8")
    assert AGENTIC_PLACEHOLDER in playbook, "the placeholder was replaced by a real value"


def test_the_published_playbook_fills_the_placeholder_in(monkeypatch):
    """Substitution happens on the way to the owner's private Drive only."""
    from stocksage.advisor import AGENTIC_PLACEHOLDER, playbook_for_publishing

    monkeypatch.setenv("STOCKSAGE_AGENTIC_ACCOUNT", "555000111")
    published = playbook_for_publishing()
    assert "555000111" in published
    assert AGENTIC_PLACEHOLDER not in published


def test_an_unset_account_leaves_the_placeholder_visible(monkeypatch):
    """Silently publishing a playbook that says '#{{AGENTIC_ACCOUNT}}' is
    safe; silently publishing one that reads like a real account is not."""
    from stocksage.advisor import AGENTIC_PLACEHOLDER, playbook_for_publishing

    monkeypatch.delenv("STOCKSAGE_AGENTIC_ACCOUNT", raising=False)
    assert AGENTIC_PLACEHOLDER in playbook_for_publishing()


def test_no_personal_name_or_home_directory():
    hits = _scan(re.compile(r"<you>|bramstedt|wisc\.edu", re.I))
    assert not hits, "the owner's identity is in a public file:\n" + "\n".join(hits)


def test_no_hardcoded_windows_user_directory():
    """C:\\Users\\<name> names the person as surely as their email does."""
    hits = _scan(
        re.compile(r"C:\\+Users\\+(?!<you>|<user>|<name>|%USERNAME%)[A-Za-z]"),
    )
    assert not hits, "a real home directory is in a public file:\n" + "\n".join(hits)


def test_no_committed_env_or_token_file():
    tracked = {p.name for p in TRACKED}
    for forbidden in (".env", "drive_token.json", "drive_credentials.json"):
        assert forbidden not in tracked, f"{forbidden} is tracked by git"


@pytest.mark.parametrize(
    "pattern,label",
    [
        (r"1//[A-Za-z0-9_-]{20,}", "Google OAuth refresh token"),
        (r"ya29\.[A-Za-z0-9_-]{20,}", "Google access token"),
        (r"gh[pousr]_[A-Za-z0-9]{30,}", "GitHub token"),
        (r"sk-[A-Za-z0-9]{20,}", "API key"),
        (r"AKIA[0-9A-Z]{16}", "AWS key"),
    ],
)
def test_no_credential_shaped_strings(pattern, label):
    hits = _scan(re.compile(pattern))
    assert not hits, f"{label} in a public file:\n" + "\n".join(hits)


def test_gitignore_still_covers_the_private_files():
    body = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in body
    assert "*.db" in body
