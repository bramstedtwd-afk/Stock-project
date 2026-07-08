import socket

from stocksage import desktop


def test_port_open_detects_listener():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert desktop.port_open(port) is True
    assert desktop.port_open(port) is False


def test_find_app_browser_returns_path_or_none():
    browser = desktop.find_app_browser()
    assert browser is None or isinstance(browser, str)


def test_main_rejects_unknown_mode(capsys):
    assert desktop.main(["bogus"]) == 2
    assert "install" in capsys.readouterr().out
