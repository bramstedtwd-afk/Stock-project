"""Government contracts and clinical trials: point in time, sized, and a broken feed is never a signal. Offline."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from stocksage import contracts as ct
from stocksage import trials as tr

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)      # a Monday


def usaspending(rows):
    def post(url, body):
        assert url == ct.USASPENDING and body["page"] >= 1
        return {"results": rows if body["page"] == 1 else [], "page_metadata": {"hasNext": False}}
    return post


ROWS = [{"Award ID": "A1", "Mod": "P00001", "Recipient Name": "CORECIVIC, INC.", "Transaction Amount": 49_801_900.0,
         "Action Date": "2026-11-20", "Awarding Agency": "Department of Homeland Security",
         "Transaction Description": "ADDITIONAL FUNDING FOR HOUSING"},
        {"Award ID": "A2", "Mod": "0", "Recipient Name": "SOUTHWEST VALLEY CONSTRUCTORS CO", "Transaction Amount": 111e6,
         "Action Date": "2026-11-19", "Awarding Agency": "Department of Homeland Security",
         "Transaction Description": "BORDER PATROL CHECKPOINT"},
        {"Award ID": "A3", "Mod": "0", "Recipient Name": "LOCKHEED MARTIN CORP", "Transaction Amount": 90e6,
         "Action Date": "2026-11-19", "Awarding Agency": "Department of Defense", "Transaction Description": "x"},
        {"Award ID": "A4", "Mod": "0", "Recipient Name": "HARRIS CORPORATION", "Transaction Amount": 56e6,
         "Action Date": "2026-11-17", "Awarding Agency": "Department of Transportation", "Transaction Description": "y"},
        {"Award ID": "A5", "Mod": "0", "Recipient Name": "TINY CO", "Transaction Amount": 1e6,
         "Action Date": "2026-11-17", "Awarding Agency": "Department of Energy", "Transaction Description": "z"}]
CAPS = {"CXW": 2.6e9, "LHX": 45e9}


def test_recipients_map_to_parents_and_private_names_are_never_guessed():
    assert ct.ticker_for("ELECTRIC BOAT CORPORATION") == ("GD", True)
    assert ct.ticker_for("HARRIS CORPORATION") == ("LHX", True)
    assert ct.ticker_for("SPACE EXPLORATION TECHNOLOGIES CORP") == (None, True)
    assert ct.ticker_for("SOUTHWEST VALLEY CONSTRUCTORS CO") == (None, False)


def test_contracts_are_sized_against_the_company_and_counted_once(tmp_path):
    state = tmp_path / "c.json"
    p = ct.packet(state, now=NOW, post=usaspending(ROWS), caps_lookup=lambda t: CAPS[t], dod=False)
    assert p["sources"]["usaspending"]["status"] == "new"
    top = p["notable"][0]
    assert top["ticker"] == "CXW" and top["share_of_value"] > 0.019, "$50M is ~2% of a $2.6B company"
    lhx = next(r for r in p["today"] if r["ticker"] == "LHX")
    assert not lhx["notable"], "$56M is 0.1% of a $45B company"
    assert not any(r["ticker"] == "LMT" for r in p["notable"] + p["today"]), "Defense rows are 90 days late: dropped"
    assert p["unmatched"][0]["recipient"] == "SOUTHWEST VALLEY CONSTRUCTORS CO"
    again = ct.packet(state, now=NOW + timedelta(hours=2), post=usaspending(ROWS), caps_lookup=lambda t: CAPS[t], dod=False)
    assert again["sources"]["usaspending"]["new"] == 0 and len(again["notable"]) == 1, "seen once, kept for days"


def test_a_feed_that_suddenly_answers_almost_nothing_is_broken_not_quiet(tmp_path):
    state = tmp_path / "c.json"
    import json

    counts = {(date(2026, 11, 23) - timedelta(days=d)).isoformat(): 40 for d in range(1, 30)}
    state.write_text(json.dumps({"counts": {"usaspending": counts}}))
    p = ct.packet(state, now=NOW, post=usaspending(ROWS[:1]), caps_lookup=lambda t: CAPS[t], dod=False)
    assert p["sources"]["usaspending"]["status"] == "broken" and not p["notable"], "1 row against a usual 40"
    h = ct.feed_health("dod", 0, {}, date(2026, 11, 23), "HTTPError: HTTP Error 403: Forbidden")
    assert h["status"] == "blocked"


def test_a_defense_announcement_page_becomes_awards():
    html = ("<h2>NAVY</h2><p>Lockheed Martin Rotary and Mission Systems, Owego, New York, is awarded a $90,152,233 "
            "firm-fixed-price modification to a previously awarded contract (N00019-24-C-0010).</p>"
            "<p>ARMY</p><p>Oshkosh Defense LLC, Oshkosh, Wisconsin, was awarded a $12,500,000 contract for trucks.</p>"
            "<p>Small Firm Inc., Austin, Texas, was awarded a $2,000,000 contract.</p>")
    rows = ct.parse_dod_article(html, "2026-11-20")
    assert [(ct.ticker_for(r["recipient"])[0], r["amount"], r["agency"]) for r in rows] == [
        ("LMT", 90_152_233.0, "Navy"), ("OSK", 12_500_000.0, "Army")]
    assert rows[0]["new_award"] is False and rows[1]["new_award"] is True


def payload(status, results=False, posted="2026-11-21", phase=("PHASE3",)):
    return {"studies": [{"protocolSection": {
        "identificationModule": {"nctId": "NCT1", "briefTitle": "A study"},
        "statusModule": {"overallStatus": status, "lastUpdatePostDateStruct": {"date": posted},
                         "whyStopped": "futility" if status == "TERMINATED" else ""},
        "designModule": {"phases": list(phase)},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme Bio Inc."}}}, "hasResults": results}]}


def test_trials_learn_first_then_report_changes(tmp_path):
    state, co = tmp_path / "t.json", {"ACME": "Acme Bio, Inc."}
    p = tr.packet(co, state, now=NOW, get=lambda url: payload("RECRUITING"))
    assert p["source"]["status"] == "ok" and p["changes"] == [], "a first look learns"
    p = tr.packet(co, state, now=NOW + timedelta(days=1), get=lambda url: payload("ACTIVE_NOT_RECRUITING"))
    assert [c["kind"] for c in p["changes"]] == ["enrolled"]
    p = tr.packet(co, state, now=NOW + timedelta(days=2), get=lambda url: payload("TERMINATED"))
    assert p["changes"][0]["kind"] == "stopped" and p["changes"][0]["why_stopped"] == "futility"


def test_a_fresh_stop_speaks_on_a_first_look_but_early_phases_never_do(tmp_path):
    co = {"ACME": "Acme Bio, Inc."}
    p = tr.packet(co, tmp_path / "a.json", now=NOW, get=lambda url: payload("TERMINATED", posted="2026-11-22"))
    assert p["changes"] and p["changes"][0]["kind"] == "stopped"
    p = tr.packet(co, tmp_path / "b.json", now=NOW, get=lambda url: payload("TERMINATED", phase=("PHASE1",)))
    assert p["changes"] == []


def test_a_registry_that_errors_for_everyone_is_blocked_not_quiet(tmp_path):
    def refuse(url):
        raise OSError("HTTP Error 403: Forbidden")
    p = tr.packet({"ACME": "Acme Bio"}, tmp_path / "t.json", now=NOW, get=refuse)
    assert p["source"]["status"] == "blocked" and p["changes"] == []


def test_several_winners_share_one_award_and_none_is_lost():
    html = ("<p>NAVY</p><p>Booz Allen Hamilton Inc., McLean, Virginia; CACI Inc., Chantilly, Virginia; and Leidos Inc., "
            "Reston, Virginia, are each awarded a $300,000,000 multiple-award contract.</p>")
    rows = ct.parse_dod_article(html, "2026-11-20")
    assert [(ct.ticker_for(r["recipient"])[0], r["amount"]) for r in rows] == [
        ("BAH", 1e8), ("CACI", 1e8), ("LDOS", 1e8)]
