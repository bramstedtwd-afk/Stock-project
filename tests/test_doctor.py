from stocksage import doctor


def test_python_and_dependency_checks_pass_here():
    assert doctor.check_python()["status"] == doctor.PASS
    # Deterministic regardless of which interpreter runs the suite:
    assert doctor.check_dependencies(("pandas", "numpy"))["status"] == doctor.PASS
    result = doctor.check_dependencies(("pandas", "not_a_real_module"))
    assert result["status"] == doctor.FAIL
    assert "not_a_real_module" in result["detail"]


def test_brain_check_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKSAGE_DB", str(tmp_path / "b.db"))
    result = doctor.check_brain()
    assert result["status"] == doctor.WARN  # healthy but not bootstrapped
    from stocksage.db import Database

    db = Database()
    db.set_meta("bootstrap_done", "2026-07-09")
    db.close()
    assert doctor.check_brain()["status"] == doctor.PASS


def test_robinhood_check_unlinked(monkeypatch):
    monkeypatch.delenv("ROBINHOOD_USERNAME", raising=False)
    monkeypatch.delenv("ROBINHOOD_PASSWORD", raising=False)
    result = doctor.check_robinhood()
    assert result["status"] == doctor.WARN
    assert "optional" in result["detail"]


def test_update_channel_detects_git_checkout():
    assert doctor.check_update_channel()["status"] == doctor.PASS


def test_run_doctor_survives_crashing_check(monkeypatch, capsys):
    def bomb():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "ALL_CHECKS", (doctor.check_python, bomb))
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert "kaboom" in out
    assert rc == 1  # crashed check reports as FAIL, not a traceback
