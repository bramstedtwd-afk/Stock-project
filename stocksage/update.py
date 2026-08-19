"""Self-update: pull the latest StockSage code from your own repository.

    ./start.sh update   (Windows: .\\start.bat update)

Improvements flow between devices the same way the brain does: push code
from one machine (or merge a pull request on GitHub), run `update`
everywhere else. The update is deliberately conservative:

  - fast-forward only — it will never rewrite or merge over local commits
  - refuses to run over uncommitted local edits (nothing silently lost)
  - shows exactly which changes came in
  - if requirements.txt changed, dependencies refresh on the next launch
    (the launcher's stamp mechanism handles it)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FETCH_RETRIES = 4  # with 2s/4s/8s/16s backoff

# Both launchers' dependency stamps; removing them triggers reinstall on launch.
_STAMPS = (".venv/.requirements.stamp", ".venv/requirements.stamp")


def say(msg: str) -> None:
    print(f"[stocksage] {msg}")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True
    )


def run_update(root: Path = PROJECT_ROOT) -> int:
    if _git(root, "rev-parse", "--git-dir").returncode != 0:
        say("This copy of StockSage is not a git checkout, so it can't self-update.")
        say("Install once with:  git clone <your-repo-url>  — then `update` works forever.")
        return 1

    if _git(root, "remote", "get-url", "origin").returncode != 0:
        say("No 'origin' remote is configured — nothing to update from.")
        return 1

    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        say("You have local edits that an update could clobber:")
        for line in dirty.stdout.strip().splitlines()[:10]:
            print("   ", line)
        say("Commit them (git commit) or discard them (git checkout -- <file>), then retry.")
        return 1

    branch_res = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_res.stdout.strip()
    if branch_res.returncode != 0 or branch == "HEAD":
        say("Not on a branch (detached HEAD) — check out your branch first.")
        return 1

    say(f"Checking your repository for updates to '{branch}'...")
    fetch_err = ""
    for attempt in range(FETCH_RETRIES):
        result = _git(root, "fetch", "origin", branch)
        if result.returncode == 0:
            break
        fetch_err = result.stderr.strip()
        if attempt < FETCH_RETRIES - 1:
            time.sleep(2 ** (attempt + 1))
    else:
        say(f"Could not reach the repository (offline?): {fetch_err}")
        return 1

    upstream = f"origin/{branch}"
    if _git(root, "rev-parse", "--verify", upstream).returncode != 0:
        say(f"The repository has no branch named '{branch}' — nothing to update from.")
        return 1

    behind = int(_git(root, "rev-list", "--count", f"HEAD..{upstream}").stdout.strip() or 0)
    ahead = int(_git(root, "rev-list", "--count", f"{upstream}..HEAD").stdout.strip() or 0)

    if behind == 0:
        version = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
        say(f"Already up to date (version {version}).")
        return 0
    if ahead > 0:
        say(
            f"This device has {ahead} local commit(s) the repository doesn't, and the "
            f"repository has {behind} this device doesn't."
        )
        say("Push your local commits first (git push), or reconcile manually — "
            "update won't guess for you.")
        return 1

    old = _git(root, "rev-parse", "HEAD").stdout.strip()
    merge = _git(root, "merge", "--ff-only", upstream)
    if merge.returncode != 0:
        say(f"Update failed: {merge.stderr.strip()}")
        return 1
    new = _git(root, "rev-parse", "HEAD").stdout.strip()

    log = _git(root, "log", "--oneline", f"{old}..{new}").stdout.strip()
    count = len(log.splitlines())
    say(f"Updated: {count} improvement(s) pulled in.")
    for line in log.splitlines()[:15]:
        print("   ", line)
    if count > 15:
        print(f"    ... and {count - 15} more")

    changed = _git(root, "diff", "--name-only", old, new).stdout
    if "requirements.txt" in changed:
        for stamp in _STAMPS:
            (root / stamp).unlink(missing_ok=True)
        say("Dependencies changed — they will refresh automatically on the next launch.")

    say("Restart StockSage to run the new version.")
    return 0


def main() -> int:
    return run_update()


if __name__ == "__main__":
    raise SystemExit(main())
