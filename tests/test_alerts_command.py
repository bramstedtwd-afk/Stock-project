"""`alerts` must work for someone who has never heard of ntfy, and never leak."""

from __future__ import annotations

import os

import pytest

from stocksage import cli, envfile, notify


@pytest.fixture(autouse=True)
def private_env(monkeypatch, tmp_path):
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC", raising=False)
    yield tmp_path / ".env"
    # save_env writes os.environ directly; do not let the topic leak into other tests.
    os.environ.pop("STOCKSAGE_NTFY_TOPIC", None)
    os.environ.pop("STOCKSAGE_PHONE_KEY", None)


def test_setup_creates_a_long_random_topic_and_saves_it_privately(capsys, private_env):
    assert cli.main(["alerts"]) == 0
    out = capsys.readouterr().out
    topic = notify.topic_from_env()
    assert topic.startswith("stocksage-") and len(topic) >= 24
    assert f"STOCKSAGE_NTFY_TOPIC={topic}" in private_env.read_text()
    assert topic in out and ".\\start.bat alerts test" in out and "\\\\" not in out
    assert "ntfy" in out and "like a password" in out


def test_running_setup_again_keeps_the_same_topic(capsys):
    cli.main(["alerts"])
    first = notify.topic_from_env()
    capsys.readouterr()
    cli.main(["alerts"])
    assert notify.topic_from_env() == first and "already set up" in capsys.readouterr().out


def test_two_setups_do_not_share_a_guessable_name(monkeypatch):
    cli.main(["alerts"])
    a = notify.topic_from_env()
    monkeypatch.delenv("STOCKSAGE_NTFY_TOPIC")
    envfile.ENV_PATH.write_text("")
    cli.main(["alerts"])
    assert a != notify.topic_from_env()


def test_the_test_push_carries_nothing_private(monkeypatch, capsys):
    cli.main(["alerts"])
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, data, headers: sent.append((url, data, headers)))
    assert cli.main(["alerts", "test"]) == 0
    url, data, headers = sent[0]
    assert url.endswith(notify.topic_from_env()) and b"alerts work" in data
    assert "Sent" in capsys.readouterr().out


def test_the_test_says_what_to_do_when_not_set_up_or_offline(monkeypatch, capsys):
    assert cli.main(["alerts", "test"]) == 1
    assert "alerts" in capsys.readouterr().out
    cli.main(["alerts"])

    def down(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(notify, "_post", down)
    assert cli.main(["alerts", "test"]) == 1
    assert "did not go through" in capsys.readouterr().out


def test_off_stops_alerts_and_setup_can_start_again(capsys):
    cli.main(["alerts"])
    assert cli.main(["alerts", "off"]) == 0
    assert notify.topic_from_env() is None
    assert "off" in capsys.readouterr().out
