"""Self-update tests against real throwaway git repositories."""

import subprocess

import pytest

from stocksage.update import run_update


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An 'origin' repo with two commits and a clone sitting one commit behind."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    (origin / "code.py").write_text("v1\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "v1")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    (origin / "code.py").write_text("v2\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "v2: improvement")
    return origin, clone


def test_update_pulls_new_commits(repos, capsys):
    origin, clone = repos
    assert run_update(root=clone) == 0
    out = capsys.readouterr().out
    assert "1 improvement(s) pulled in" in out
    assert "v2: improvement" in out
    assert (clone / "code.py").read_text() == "v2\n"


def test_update_already_current(repos, capsys):
    _, clone = repos
    run_update(root=clone)
    capsys.readouterr()
    assert run_update(root=clone) == 0
    assert "Already up to date" in capsys.readouterr().out


def test_update_refuses_dirty_tree(repos, capsys):
    _, clone = repos
    (clone / "code.py").write_text("my local experiment\n")
    assert run_update(root=clone) == 1
    assert "local edits" in capsys.readouterr().out
    # Nothing was lost:
    assert (clone / "code.py").read_text() == "my local experiment\n"


def test_update_refuses_diverged_history(repos, capsys):
    """Genuine local work: pushing it is the right advice."""
    _, clone = repos
    (clone / "local.py").write_text("x\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "local-only work")
    assert run_update(root=clone) == 1
    out = capsys.readouterr().out
    assert "local commit" in out
    assert "genuinely new work" in out and "git push" in out
    assert "reset --hard" not in out, "must not offer to discard real work"


def test_rewritten_history_says_reset_not_push(repos, capsys):
    """After a history rewrite upstream (here: the privacy scrub that
    rewrote every commit), the device's commits are stale twins of commits
    that already exist upstream. Telling the owner to push them would put
    the removed content straight back — the one thing the rewrite existed
    to prevent."""
    origin, clone = repos
    # The clone holds the pre-rewrite copy of v1; origin rewrites it so the
    # same patch lives under a different commit id, then moves on.
    git(clone, "fetch", "origin")
    git(origin, "commit", "--amend", "-m", "v2: improvement (rewritten)")
    (origin / "more.py").write_text("later\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "v3")
    git(clone, "fetch", "origin")
    git(clone, "reset", "--hard", "origin/main")
    # Now put the clone back on the OLD commit id, as a real device would be.
    git(clone, "reset", "--hard", "HEAD~2")
    (clone / "code.py").write_text("v2\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "v2: improvement")

    assert run_update(root=clone) == 1
    out = capsys.readouterr().out
    assert "rewritten" in out
    assert "reset --hard" in out
    assert "Do NOT push" in out


def test_update_requirements_change_clears_stamps(repos, capsys):
    origin, clone = repos
    stamp = clone / ".venv" / ".requirements.stamp"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("old")
    (origin / "requirements.txt").write_text("pandas\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "add dependency")
    assert run_update(root=clone) == 0
    assert not stamp.exists()
    assert "Dependencies changed" in capsys.readouterr().out


def test_update_outside_git_checkout(tmp_path, capsys):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert run_update(root=plain) == 1
    assert "not a git checkout" in capsys.readouterr().out
