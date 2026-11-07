"""The test suite itself must never reach the owner's real things.

2026-11-06: run on the always-on PC, the suite loaded the real .env and pushed
fake "SELL AAA - hit its stop" alerts to the owner's phone. conftest now blocks
every outside connection and hides the real .env and secrets; these tests make
sure that guard stays in place."""

import os
import urllib.request

import pytest

from stocksage import envfile, notify


def test_an_outside_connection_fails_loudly():
    with pytest.raises(RuntimeError, match="tests are offline"):
        urllib.request.urlopen("https://ntfy.sh/anything", timeout=2)


def test_the_real_env_file_is_never_the_one_tests_use():
    assert envfile.ENV_PATH != envfile.PROJECT_ROOT / ".env"
    assert not envfile.load_env(), "a test sees no .env unless it writes its own"


def test_no_secret_reaches_a_test_even_after_the_cli_loads_its_env():
    from stocksage import cli

    try:
        cli.main(["--help"])          # main() loads .env before it even parses the arguments
    except SystemExit:
        pass
    leaked = [k for k in os.environ if k.startswith(("ROBINHOOD_", "STOCKSAGE_NTFY", "STOCKSAGE_TRADER", "STOCKSAGE_DRIVE"))]
    assert not leaked


def test_an_alert_has_nowhere_to_go():
    assert not (os.environ.get("STOCKSAGE_NTFY_TOPIC") or "").strip()
    with pytest.raises(RuntimeError, match="tests are offline"):
        notify._post(notify.NTFY_URL.format(topic="guard-check"), b"x", {})
