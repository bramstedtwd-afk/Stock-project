"""The cloud runner is public, unattended, and holds a Google token. Its file
is checked for the mistakes that would be invisible until they mattered: a
cron typo that quietly switches learning off, a secret echoed into a public
log, a broker credential creeping in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "cloud-research.yml"
TEXT = PATH.read_text(encoding="utf-8")
WF = yaml.safe_load(TEXT)
TRIGGERS = WF.get("on", WF.get(True))
JOB = WF["jobs"]["research"]
STEPS = JOB["steps"]
CRONS = [t["cron"] for t in TRIGGERS["schedule"]]


def _quoted_crons(expr: str) -> list[str]:
    """Quoted strings in an `if` that are cron expressions (five fields)."""
    return [q for q in re.findall(r"'([^']+)'", expr) if len(q.split()) == 5]


def _step(fragment: str) -> dict:
    return next(s for s in STEPS if fragment in s.get("name", ""))


def test_it_runs_on_a_schedule_and_by_hand():
    assert set(TRIGGERS) == {"schedule", "workflow_dispatch"}
    assert len(CRONS) == 6


def test_every_cron_is_a_valid_weekday_only_time():
    for cron in CRONS:
        minute, hour, dom, month, dow = cron.split()
        assert 0 <= int(minute) < 60 and 0 <= int(hour) < 24
        assert (dom, month) == ("*", "*")
        assert dow == "1-5", f"{cron} would run on a weekend, when markets are shut"


def test_the_learning_step_is_tied_to_a_cron_that_actually_exists():
    """If the string in the `if` ever stops matching a real schedule entry,
    learning silently never runs — and nothing fails to say so."""
    step = _step("Learn")
    quoted = _quoted_crons(step["if"])
    assert quoted, "the learn step has no schedule condition"
    for cron in quoted:
        assert cron in CRONS, f"learn step waits for '{cron}', which is not scheduled"
    assert "workflow_dispatch" in step["if"], "a manual run should also learn"


def test_exactly_one_scheduled_run_a_day_learns():
    assert len(_quoted_crons(_step("Learn")["if"])) == 1


def test_it_never_holds_a_broker_credential():
    """A public repo, public logs. The cloud does market-side work only.
    Checked against the PARSED workflow — what actually runs — so a comment
    explaining why there are no broker secrets cannot trip it."""
    parsed = yaml.dump(WF).upper()
    for word in ("ROBINHOOD", "ROBIN_STOCKS", "MFA", "TOTP", "PASSWORD"):
        assert word not in parsed, f"{word} appears in what the workflow runs"


def test_the_publish_step_never_opens_the_broker():
    assert "--no-broker" in _step("Publish")["run"]


def test_only_the_drive_token_and_an_optional_healthcheck_are_secrets():
    used = set(re.findall(r"secrets\.([A-Z_]+)", TEXT))
    assert used == {"STOCKSAGE_DRIVE_TOKEN", "STOCKSAGE_HEALTHCHECK_URL"}


def test_secrets_are_never_printed():
    for step in STEPS:
        run = step.get("run", "")
        assert "set -x" not in run and "printenv" not in run
        assert not re.search(r"\becho\b[^\n]*\$\{?STOCKSAGE_DRIVE_TOKEN", run)
        assert not re.search(r"^\s*env\s*$", run, flags=re.M)


def test_the_token_is_passed_as_an_environment_variable_not_pasted_into_a_command():
    for step in STEPS:
        assert "secrets." not in step.get("run", ""), "a secret interpolated into a shell line"


def test_permissions_are_read_only():
    assert WF["permissions"] == {"contents": "read"}


def test_runs_are_serialised_and_never_cancelled_mid_write():
    c = WF["concurrency"]
    assert c["cancel-in-progress"] is False and c["group"]


def test_a_runaway_job_is_cut_off():
    assert 5 <= JOB["timeout-minutes"] <= 60


def test_third_party_actions_are_pinned_to_a_released_version():
    for step in STEPS:
        ref = step.get("uses")
        if ref:
            assert re.search(r"@v\d+$", ref), f"{ref} is not pinned to a release"


def test_a_missing_token_fails_loudly_with_the_fix():
    step = _step("Drive sign-in")
    assert "exit 1" in step["run"] and "SETUP.md" in step["run"]


def test_the_healthcheck_pings_only_after_success():
    step = _step("dead-man")
    assert "success()" in step["if"]


def test_the_steps_run_in_the_order_that_keeps_the_brain_consistent():
    names = [s["name"] for s in STEPS if "name" in s]
    pull = next(i for i, n in enumerate(names) if "Pull" in n)
    learn = next(i for i, n in enumerate(names) if "Learn" in n)
    publish = next(i for i, n in enumerate(names) if "Publish" in n)
    assert pull < learn < publish, "learn on the shared brain, then publish what it learned"
