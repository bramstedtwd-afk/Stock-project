from stocksage import universe


def test_ten_sectors_ten_tickers_each():
    assert len(universe.SECTORS) == 10
    for sector in universe.SECTORS:
        assert len(sector.tickers) == 10, sector.name
        assert sector.etf.startswith("XL")


def test_no_duplicate_tickers_within_sector():
    for sector in universe.SECTORS:
        assert len(set(sector.tickers)) == len(sector.tickers)


def test_all_tickers_deduplicated():
    tickers = universe.all_tickers()
    assert len(tickers) == len(set(tickers))
    assert len(tickers) == 100


def test_sector_lookup():
    assert universe.sector_of("AAPL").name == "Technology"
    assert universe.sector_of("ZZZZ") is None
    assert universe.sector_by_name("energy").etf == "XLE"
