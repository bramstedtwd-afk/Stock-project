from stocksage import autopilot


def test_cron_line_runs_daily_on_weekdays():
    line = autopilot.cron_line()
    assert line.startswith(f"{autopilot.RUN_MINUTE} {autopilot.RUN_HOUR} * * 1-5 ")
    assert "start.sh" in line and "daily" in line
    assert autopilot.CRON_TAG in line


def test_launchd_plist_shape():
    plist = autopilot.launchd_plist()
    assert autopilot.LAUNCHD_LABEL in plist
    assert plist.count("<key>Weekday</key>") == 5
    assert "start.sh" in plist and "<string>daily</string>" in plist


def test_schtasks_cmd_shape():
    cmd = autopilot.schtasks_create_cmd()
    assert cmd[0] == "schtasks" and "/Create" in cmd
    assert "MON,TUE,WED,THU,FRI" in cmd
    assert any("start.bat" in part for part in cmd)


def test_main_rejects_unknown(capsys):
    assert autopilot.main(["bogus"]) == 2


# --- publish job builders ---

def test_publish_cron_lines_one_per_time():
    lines = autopilot.publish_cron_lines("/Users/me/GDrive/StockSage")
    assert len(lines) == len(autopilot.PUBLISH_TIMES)
    for (h, m), line in zip(autopilot.PUBLISH_TIMES, lines):
        assert line.startswith(f"{m} {h} * * 1-5 ")
        assert "publish" in line and "StockSage" in line
        assert autopilot.PUBLISH_CRON_TAG in line


def test_publish_launchd_plist_covers_all_times_and_weekdays():
    plist = autopilot.publish_launchd_plist("/Users/me/GDrive/StockSage")
    assert autopilot.PUBLISH_LAUNCHD_LABEL in plist
    assert plist.count("<key>Weekday</key>") == 5 * len(autopilot.PUBLISH_TIMES)
    assert "<string>publish</string>" in plist
    assert "<string>/Users/me/GDrive/StockSage</string>" in plist


def test_publish_schtasks_cmds_one_task_per_time():
    cmds = autopilot.publish_schtasks_cmds("C:/GDrive/StockSage")
    assert len(cmds) == len(autopilot.PUBLISH_TIMES)
    names = [cmds[i][cmds[i].index("/TN") + 1] for i in range(len(cmds))]
    assert len(set(names)) == len(cmds)  # distinct task names per time
    assert all("publish" in " ".join(c) for c in cmds)


def test_publish_requires_folder_arg(capsys):
    assert autopilot.main(["publish"]) == 2
    assert "Usage" in capsys.readouterr().out


# --- publish-drive: no-install, direct-API variant (folder=None) ---

def test_publish_cron_lines_none_uses_publish_drive_subcommand():
    lines = autopilot.publish_cron_lines(None)
    assert len(lines) == len(autopilot.PUBLISH_TIMES)
    for line in lines:
        assert "publish-drive" in line
        assert '"' not in line.split("publish-drive")[1].split(">>")[0]  # no folder arg tacked on


def test_publish_launchd_plist_none_uses_publish_drive():
    plist = autopilot.publish_launchd_plist(None)
    assert "<string>publish-drive</string>" in plist
    assert "<string>publish</string>" not in plist  # not the folder variant


def test_publish_schtasks_cmds_none_uses_publish_drive():
    cmds = autopilot.publish_schtasks_cmds(None)
    assert len(cmds) == len(autopilot.PUBLISH_TIMES)
    for cmd in cmds:
        tr = cmd[cmd.index("/TR") + 1]
        assert "publish-drive" in tr


def test_turn_on_publish_none_requires_drive_credentials(tmp_path, monkeypatch, capsys):
    from stocksage import drive_api

    monkeypatch.setattr(drive_api, "CREDENTIALS_PATH", tmp_path / "missing.json")
    assert autopilot.turn_on_publish(None) == 1
    assert "isn't set up" in capsys.readouterr().out.lower()


def test_main_publish_drive_routes_correctly(tmp_path, monkeypatch, capsys):
    from stocksage import drive_api

    monkeypatch.setattr(drive_api, "CREDENTIALS_PATH", tmp_path / "missing.json")
    assert autopilot.main(["publish-drive"]) == 1  # reaches turn_on_publish(None)
    assert "isn't set up" in capsys.readouterr().out.lower()


# --- self-update: opt-in only -----------------------------------------------------

def test_self_update_runs_the_safe_update_command_on_weekdays_before_publishing():
    from stocksage import autopilot as ap

    cmd = ap.update_schtasks_cmd()
    assert cmd[-1] == "08:10" and "MON,TUE,WED,THU,FRI" in cmd
    tr = cmd[cmd.index("/TR") + 1]
    assert tr.endswith('start.bat" update'), "the update branch exits before cmd reads further"
    assert ap.UPDATE_TIME < min(ap.PUBLISH_TIMES)
    assert "update" in ap.update_cron_line() and ap.UPDATE_CRON_TAG in ap.update_cron_line()
    assert "<string>update</string>" in ap.update_launchd_plist()


def test_self_update_is_only_ever_switched_on_by_the_owners_command():
    """It runs whatever is on GitHub on the computer holding the broker login, so
    no code path may turn it on except `autopilot auto-update`."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "stocksage"
    callers = [p.name for p in root.glob("*.py") if re.search(r"turn_on_update\(", p.read_text(encoding="utf-8"))]
    assert callers == ["autopilot.py"]
    text = (root / "autopilot.py").read_text(encoding="utf-8")
    assert text.count("turn_on_update(") == 2, "its definition and the auto-update command, nothing else"


def test_autopilot_off_also_removes_self_update(monkeypatch):
    from stocksage import autopilot as ap

    monkeypatch.setattr(ap.sys, "platform", "linux")
    written = {}
    tab = f"{ap.cron_line()}\n{ap.update_cron_line()}\nother job\n"
    monkeypatch.setattr(ap, "_current_crontab", lambda: tab)
    monkeypatch.setattr(ap, "_write_crontab", lambda content, fallback_lines=None: written.setdefault("c", content) or True)
    ap.turn_off()
    assert ap.UPDATE_CRON_TAG not in written["c"] and "other job" in written["c"]
