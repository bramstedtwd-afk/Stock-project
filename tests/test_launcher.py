"""The launcher must rebuild an environment whose Python has gone missing.

A virtualenv is a pointer to the interpreter it was built from. On the
owner's second machine that interpreter lived in a Downloads folder that
was later cleared, leaving `.venv\\Scripts\\python.exe` in place but dead:
every command died with "No Python at ...", and because the launcher only
asked whether that file *existed*, it never rebuilt, and `update` — the
command that would have delivered the fix — could not run either.

The shell guard is executed for real (it is the actual text of start.sh).
The batch guard cannot run on Linux, so it is checked structurally,
including the one mistake that breaks a .bat block without any error.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _between(text: str, begin: str, end: str) -> str:
    start = text.index(begin) + len(begin)
    return text[start: text.index(end)]


def _fake_python(path: Path, works: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexit {0 if works else 1}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_guard(workdir: Path) -> str:
    guard = _between(
        (ROOT / "start.sh").read_text(encoding="utf-8"),
        "# BEGIN heal-venv", "# END heal-venv",
    )
    script = "say() { echo \"$*\"; }\n" + guard
    result = subprocess.run(
        ["bash", "-c", script], cwd=workdir, capture_output=True, text=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(os.name == "nt", reason="runs the POSIX launcher")
def test_a_dead_environment_is_removed_so_it_can_be_rebuilt(tmp_path):
    _fake_python(tmp_path / ".venv" / "bin" / "python", works=False)
    out = _run_guard(tmp_path)
    assert not (tmp_path / ".venv").exists(), "a venv with no Python behind it survived"
    assert "rebuilding" in out


@pytest.mark.skipif(os.name == "nt", reason="runs the POSIX launcher")
def test_a_working_environment_is_never_touched(tmp_path):
    """The guard must not turn a healthy install into a slow rebuild."""
    _fake_python(tmp_path / ".venv" / "bin" / "python", works=True)
    marker = tmp_path / ".venv" / "keep-me"
    marker.write_text("x", encoding="utf-8")
    out = _run_guard(tmp_path)
    assert marker.exists()
    assert out == ""


@pytest.mark.skipif(os.name == "nt", reason="runs the POSIX launcher")
def test_no_environment_at_all_is_left_for_first_run(tmp_path):
    assert _run_guard(tmp_path) == ""
    assert not (tmp_path / ".venv").exists()


@pytest.mark.skipif(os.name == "nt", reason="runs the POSIX launcher")
def test_a_dead_environment_does_not_kill_a_set_e_launcher(tmp_path):
    """start.sh runs under `set -e`; a failing probe inside the guard must be
    an answer, not an abort."""
    guard = _between(
        (ROOT / "start.sh").read_text(encoding="utf-8"),
        "# BEGIN heal-venv", "# END heal-venv",
    )
    _fake_python(tmp_path / ".venv" / "bin" / "python", works=False)
    script = "set -euo pipefail\nsay() { echo \"$*\"; }\n" + guard + "\necho continued\n"
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0 and "continued" in result.stdout


# --- the Windows launcher: structure, since it cannot be executed here ------


def _bat() -> str:
    return (ROOT / "start.bat").read_text(encoding="utf-8")


def test_the_batch_guard_runs_before_the_exists_check():
    bat = _bat()
    guard_at = bat.index("rem BEGIN heal-venv")
    assert guard_at < bat.index("if not exist .venv\\Scripts\\python.exe"), (
        "the heal step must come first or the dead venv is never rebuilt"
    )


def test_the_batch_guard_actually_probes_the_interpreter():
    guard = _between(_bat(), "rem BEGIN heal-venv", "rem END heal-venv")
    assert '.venv\\Scripts\\python.exe -c "import sys"' in guard
    assert "if errorlevel 1" in guard
    assert "rmdir /s /q .venv" in guard


def test_no_parenthesis_inside_the_batch_guard_message():
    """A ')' inside an echo within a parenthesised block closes the block
    early. It does not error — the rest of the guard just stops applying —
    which is the kind of failure nobody notices until it is needed."""
    guard = _between(_bat(), "rem BEGIN heal-venv", "rem END heal-venv")
    for line in guard.splitlines():
        if line.strip().startswith("echo"):
            assert "(" not in line and ")" not in line, line
    opens = len(re.findall(r"\($", guard, flags=re.M))
    closes = len(re.findall(r"^\s*\)\s*$", guard, flags=re.M))
    assert opens == closes, "unbalanced parentheses in the batch guard"
