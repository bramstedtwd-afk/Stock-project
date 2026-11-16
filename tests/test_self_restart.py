"""The always-on computer restarts its own watcher and phone page from the scheduled
publish job: on 2026-11-15 both stopped with no restart and nothing noticed for 19 hours."""

from __future__ import annotations

import sys
import time

import pytest

from stocksage import finnhub, mobile, watcher

windows_only = pytest.mark.skipif(not sys.platform.startswith("win"), reason="the Startup folder is Windows")


def installed(monkeypatch, name: str) -> None:
    folder = mobile.startup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("@echo off\r\n", encoding="utf-8")
    for var in (watcher.URL_VAR, watcher.TOKEN_VAR, finnhub.KEY_VAR):
        monkeypatch.setenv(var, "set-for-the-test")


@windows_only
def test_a_stopped_watcher_is_started_again_and_a_running_one_is_left_alone(monkeypatch):
    installed(monkeypatch, watcher.STARTUP_FILE)
    started = []
    start = lambda: started.append(1) or True  # noqa: E731
    watcher.save_state({"checked": "now"})
    now = time.time()
    assert not watcher.revive(now + 60, start) and not started, "wrote a minute ago: running"
    assert watcher.revive(now + watcher.REVIVE_AFTER + 1, start) and started == [1]
    watcher.state_path().unlink()
    assert watcher.revive(now, start), "installed but never wrote a state file: start it"


@windows_only
def test_a_watcher_the_owner_switched_off_or_without_keys_is_never_started(monkeypatch):
    started = []
    start = lambda: started.append(1) or True  # noqa: E731
    late = time.time() + 10 * watcher.REVIVE_AFTER
    assert not watcher.revive(late, start), "never installed here"
    installed(monkeypatch, watcher.STARTUP_FILE)
    monkeypatch.delenv(watcher.TOKEN_VAR)
    assert not watcher.revive(late, start), "no keys: it could not work"
    monkeypatch.setenv(watcher.TOKEN_VAR, "x")
    (mobile.startup_dir() / watcher.STARTUP_FILE).unlink()          # .\start.bat watch-trader off
    assert not watcher.revive(late, start) and not started

    def broken():
        raise OSError("no window")

    installed(monkeypatch, watcher.STARTUP_FILE)
    assert not watcher.revive(late, broken), "a failed start never raises into the publish job"


@windows_only
def test_the_phone_page_is_started_only_when_installed_and_not_answering(monkeypatch):
    started = []
    start = lambda: started.append(1) or True  # noqa: E731
    assert not mobile.revive(start, lambda: False), "never installed here"
    installed(monkeypatch, mobile.STARTUP_FILE)
    assert not mobile.revive(start, lambda: True) and not started, "already answering"
    assert mobile.revive(start, lambda: False) and started == [1]


def test_tests_never_see_the_owners_real_startup_folder(tmp_path):
    assert str(tmp_path) in str(mobile.startup_dir())
