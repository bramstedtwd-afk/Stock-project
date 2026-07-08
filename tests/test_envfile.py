import os

from stocksage.envfile import load_env, save_env


def test_load_env_parses_and_respects_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "ROBINHOOD_USERNAME=me@example.com\n"
        "QUOTED='secret value'\n"
        "EMPTY=\n"
        "not a kv line\n"
    )
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)
    monkeypatch.setenv("QUOTED", "already-set")
    loaded = load_env(env)
    assert loaded["ROBINHOOD_USERNAME"] == "me@example.com"
    assert os.environ["ROBINHOOD_USERNAME"] == "me@example.com"
    assert os.environ["QUOTED"] == "already-set"  # real env wins
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)


def test_load_env_missing_file(tmp_path):
    assert load_env(tmp_path / "nope.env") == {}


def test_save_env_merges_and_is_private(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("KEEP=1\nROBINHOOD_USERNAME=old\n")
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)
    save_env({"ROBINHOOD_USERNAME": "new@example.com", "SKIPPED": None}, env)
    text = env.read_text()
    assert "KEEP=1" in text
    assert "ROBINHOOD_USERNAME=new@example.com" in text
    assert "SKIPPED" not in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert os.environ["ROBINHOOD_USERNAME"] == "new@example.com"
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)


def test_empty_values_do_not_count_as_credentials(tmp_path, monkeypatch):
    """The .env template ships with blank values; they must not read as linked."""
    from stocksage.robinhood import RobinhoodClient

    env = tmp_path / ".env"
    env.write_text("ROBINHOOD_USERNAME=\nROBINHOOD_PASSWORD=\n")
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)
    monkeypatch.delenv("ROBINHOOD_PASSWORD", raising=False)
    load_env(env)
    assert not RobinhoodClient.credentials_available()
