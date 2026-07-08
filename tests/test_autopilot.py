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
