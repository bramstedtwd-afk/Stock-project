from stocksage import desktop


def test_lan_ip_shape():
    ip = desktop.lan_ip()
    if ip is not None:  # machines without network return None, also valid
        parts = ip.split(".")
        assert len(parts) == 4 and all(p.isdigit() for p in parts)
        assert not ip.startswith("127.")


def test_phone_mode_fails_cleanly_without_network(monkeypatch):
    monkeypatch.setattr(desktop, "lan_ip", lambda: None)
    assert desktop.run_phone() == 1


def test_qr_prints_ascii(capsys):
    desktop._print_qr("http://192.168.1.20:8531")
    out = capsys.readouterr().out
    assert out.strip()  # either a QR block or the install hint
