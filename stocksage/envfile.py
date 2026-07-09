"""Tiny .env loader — no python-dotenv dependency needed.

Looks for a .env next to the project root (the directory containing this
package) and loads KEY=VALUE lines into os.environ without overriding
variables already set in the real environment. Also provides the writer the
dashboard uses to save credentials, kept private with 0600 permissions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_env(path: Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs; real environment always wins. Returns what was read."""
    path = path or ENV_PATH
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):  # shell-style .env files work too
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not key or " " in key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def save_env(values: dict[str, str], path: Path | None = None) -> None:
    """Merge values into the .env file, creating it private (0600)."""
    path = path or ENV_PATH
    existing = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in values.items() if v is not None})
    body = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    path.write_text(body)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    # Make the new values visible to this process immediately.
    for k, v in values.items():
        if v:
            os.environ[k] = v
