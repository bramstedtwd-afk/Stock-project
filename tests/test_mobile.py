"""The phone page is reachable by anything on the Wi-Fi, so it must be locked,
read-only, and unable to leak the key or anything else."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from stocksage import envfile, mobile


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("STOCKSAGE_STATE", str(tmp_path))
    monkeypatch.setenv(mobile.KEY_VAR, "k" * 32)
    return tmp_path


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mobile.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_the_right_key_shows_the_saved_sheet(server, isolated):
    (isolated / "today.txt").write_text("STOCKSAGE TODAY\nSELL ALL DE <b>&", encoding="utf-8")
    status, body, headers = get(f"{server}/?k={'k' * 32}")
    assert status == 200 and "SELL ALL DE" in body
    assert "<b>" not in body and "&lt;b&gt;&amp;" in body, "sheet text must be escaped, never run as HTML"
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/?k=wrong", "/?k=", "/?k=" + "k" * 31, "/today.txt?k=" + "k" * 32,
                                  "/../../.env?k=" + "k" * 32])
def test_everything_without_the_exact_key_is_refused(server, isolated, path):
    (isolated / "today.txt").write_text("SECRET SHEET", encoding="utf-8")
    status, body, _ = get(server + path)
    assert status == 403 and "SECRET" not in body


def test_with_no_key_configured_nobody_gets_in(server, monkeypatch, isolated):
    monkeypatch.delenv(mobile.KEY_VAR)
    (isolated / "today.txt").write_text("SECRET SHEET", encoding="utf-8")
    assert get(f"{server}/?k=anything")[0] == 403
    assert not mobile.key_ok("", None) and not mobile.key_ok(None, "x")


def test_it_is_read_only(server):
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        req = urllib.request.Request(f"{server}/?k={'k' * 32}", data=b"x", method=method)
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 405


def test_the_key_is_never_written_to_a_log(server, capfd):
    get(f"{server}/?k={'k' * 32}")
    out = capfd.readouterr()
    assert "k" * 32 not in out.out + out.err


def test_no_sheet_yet_says_how_to_make_one():
    page = mobile.render_page()
    assert "No sheet yet" in page and ".\\start.bat today" in page and "\\\\" not in page


def test_an_old_sheet_warns_that_the_computer_stopped_refreshing(isolated):
    f = isolated / "today.txt"
    f.write_text("old", encoding="utf-8")
    assert "old. The computer has not refreshed" not in mobile.render_page(now=time.time() + 3600)
    assert "day(s) old" in mobile.render_page(now=time.time() + 4 * 86400)


def test_the_page_works_on_a_phone_in_light_and_dark():
    page = mobile.render_page()
    assert 'name="viewport"' in page and "prefers-color-scheme: dark" in page


def test_the_key_is_created_once_and_saved_privately(monkeypatch, isolated):
    monkeypatch.delenv(mobile.KEY_VAR)
    first = mobile.phone_key(create=True)
    assert len(first) >= 30 and f"{mobile.KEY_VAR}={first}" in (isolated / ".env").read_text()
    assert mobile.phone_key(create=True) == first


def test_the_link_carries_the_key_and_a_port():
    found = mobile.links()
    assert found and all(f"?k={'k' * 32}" in u and f":{mobile.PORT}/" in u for u in found)


def test_windows_start_by_itself_at_sign_in_through_the_launcher():
    cmd = mobile.schtasks_create_cmd()
    assert cmd[:3] == ["schtasks", "/Create", "/F"] and "ONLOGON" in cmd
    assert "start.bat" in cmd[-1] and cmd[-1].endswith("mobile serve")


def test_the_mac_and_linux_entries_restart_it():
    assert "<key>KeepAlive</key><true/>" in mobile.launchd_plist() and "mobile" in mobile.launchd_plist()
    assert mobile.cron_line().startswith("@reboot") and mobile.CRON_TAG in mobile.cron_line()


def test_the_command_is_wired_and_prints_a_link(capsys):
    from stocksage import cli

    assert cli.main(["mobile"]) == 0
    out = capsys.readouterr().out
    assert "?k=" in out and ".\\start.bat mobile install" in out and "\\\\" not in out
