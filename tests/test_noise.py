"""Terminal output the owner can actually read.

A run that succeeds must not look like a failure. One delisted ticker in a
portfolio produced dozens of red ERROR lines from yfinance around a line
saying 187 trades were captured — the scary part is louder than the result,
and a non-technical owner reasonably concludes something is broken.

Silence alone is not the fix either: a name that cannot be priced is
silently missing from the analysis, and the owner should be told once, in
their own language, with the way to act on it.
"""

from __future__ import annotations

import logging

import pytest

from stocksage import cli, data


@pytest.fixture(autouse=True)
def clean_slate():
    data.clear_unpriceable()
    root = logging.getLogger()
    saved = [(h, list(h.filters)) for h in root.handlers]
    yield
    data.clear_unpriceable()
    for handler, filters in saved:
        handler.filters = filters


def test_yfinance_chatter_is_dropped_at_the_handler(caplog):
    cli._silence_yfinance()
    for handler in logging.getLogger().handlers:
        record = logging.LogRecord(
            "yfinance", logging.ERROR, __file__, 1,
            "$BMWYY: possibly delisted; no price data found", None, None,
        )
        assert not all(f.filter(record) for f in handler.filters)


def test_silencing_survives_yfinance_resetting_its_own_level():
    """yfinance assigns DEBUG/NOTSET to its logger in its debug helpers, so a
    level we set at startup can be thrown away mid-run. The filter cannot."""
    cli._silence_yfinance()
    logging.getLogger("yfinance").setLevel(logging.NOTSET)  # what yfinance does

    record = logging.LogRecord(
        "yfinance", logging.ERROR, __file__, 1, "possibly delisted", None, None
    )
    handlers = logging.getLogger().handlers
    assert handlers, "no root handler to filter on"
    for handler in handlers:
        assert not all(f.filter(record) for f in handler.filters)


def test_our_own_warnings_still_come_through():
    """Suppressing yfinance must not suppress StockSage."""
    cli._silence_yfinance()
    record = logging.LogRecord(
        "stocksage.engine", logging.WARNING, __file__, 1, "real problem", None, None
    )
    for handler in logging.getLogger().handlers:
        assert all(f.filter(record) for f in handler.filters)


def test_a_skipped_ticker_is_reported_once_with_the_fix(capsys):
    data.note_unpriceable("bmwyy")
    data.note_unpriceable("BMWYY")  # same name twice in a run
    cli._report_unpriceable()
    out = capsys.readouterr().out

    assert out.count("BMWYY") >= 1
    assert "no price data for BMWYY" in out
    assert ".\\start.bat watch remove BMWYY" in out
    assert "delisted, renamed, or not carried" in out


def test_nothing_is_printed_on_a_clean_run(capsys):
    cli._report_unpriceable()
    assert capsys.readouterr().out == ""


def test_several_missing_names_are_listed_together(capsys):
    for ticker in ("BMWYY", "AAAA", "ZZZZ"):
        data.note_unpriceable(ticker)
    cli._report_unpriceable()
    out = capsys.readouterr().out
    for ticker in ("BMWYY", "AAAA", "ZZZZ"):
        assert ticker in out
    assert out.count("Note: no price data") == 1, "one summary, not one per name"


def test_history_records_a_name_it_could_not_price(tmp_path, monkeypatch):
    """The end-of-run note is only honest if the data layer actually reports."""
    market = data.MarketData(cache_dir=tmp_path)
    monkeypatch.setattr(market, "_download", lambda *a, **k: None)
    assert market.history("BMWYY") is None
    assert "BMWYY" in data.unpriceable()


def test_a_stale_cache_hit_is_not_reported_as_missing(tmp_path, monkeypatch):
    """Falling back to yesterday's data still produces signals, so it is not
    something to interrupt the owner about."""
    from tests.conftest import make_ohlcv

    market = data.MarketData(cache_dir=tmp_path)
    market._write_cache("AAPL", data.HISTORY_PERIOD, make_ohlcv(120))
    monkeypatch.setattr(market, "_download", lambda *a, **k: None)
    monkeypatch.setattr(market, "_read_cache",
                        lambda t, p, ignore_ttl=False: make_ohlcv(120) if ignore_ttl else None)
    assert market.history("AAPL") is not None
    assert "AAPL" not in data.unpriceable()


def test_the_report_runs_even_when_the_command_fails(monkeypatch, capsys):
    """A crash is exactly when the owner most needs to know what was skipped."""
    data.note_unpriceable("BMWYY")

    def boom(args):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(cli, "_report_unpriceable", cli._report_unpriceable)
    parser_args = ["watch", "list"]
    monkeypatch.setattr("stocksage.cli.cmd_watch", boom)
    with pytest.raises(RuntimeError):
        cli.main(parser_args)
    assert "BMWYY" in capsys.readouterr().out
