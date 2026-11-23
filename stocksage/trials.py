"""Clinical trials: status changes at drug companies the trader holds or is looking at.

A late-stage trial stopped early (suspended, terminated, withdrawn), finished enrolling,
completed, or posting its results moves a drug company's stock, and ClinicalTrials.gov
(the US National Library of Medicine's free, official registry) often shows the change
before a press release says much. This watches Phase 2/3 and Phase 3 studies whose lead
sponsor is one of the given companies.

Honesty details:
  * Forward only. The registry keeps no clean record of when each status became public,
    so this cannot be tested on the past without hindsight; it is logged and graded from
    now on (IDEA lines, setup word `trial`).
  * A change is what differs from the last look (`state`); the first look at a company
    only learns, except a stop or results posted in the last 3 days, which matter whenever
    they are seen.
  * Feed health: if the registry answers with errors for every company, the watch says so
    (blocked/broken) instead of reporting "no changes".

Context, never an order. Network lives in `_get_json`; the rest is pure and tested offline.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

API = ("https://clinicaltrials.gov/api/v2/studies?query.spons={sponsor}"
       "&filter.advanced=AREA%5BLastUpdatePostDate%5DRANGE%5B{since},MAX%5D"
       "&fields=NCTId,BriefTitle,OverallStatus,Phase,LastUpdatePostDate,LeadSponsorName,WhyStopped,HasResults"
       "&pageSize=100")
LOOKBACK_DAYS = 30
FRESH_DAYS = 3                  # a stop or results posted this recently matter even on a first look
KEEP_DAYS = 7                   # changes stay in the brief this long
LATE = {"PHASE3", "PHASE2/PHASE3"}
STOPPED = {"SUSPENDED", "TERMINATED", "WITHDRAWN"}
FINISHING = {"ACTIVE_NOT_RECRUITING", "COMPLETED"}
MAX_COMPANIES = 25


def _get_json(url: str) -> dict:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "StockSage research"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def sponsor_name(name: str) -> str:
    """'Vertex Pharmaceuticals Incorporated' -> 'Vertex Pharmaceuticals' (what the registry matches on)."""
    from .news import clean_name

    return clean_name(name or "").strip()


def studies(payload: dict) -> list[dict]:
    """The registry's answer -> flat rows for late-stage studies only."""
    out = []
    for s in payload.get("studies") or []:
        p = s.get("protocolSection") or {}
        status = p.get("statusModule") or {}
        phases = (p.get("designModule") or {}).get("phases") or []
        phase = "/".join(phases)
        if phase not in LATE:
            continue
        out.append({"nct": (p.get("identificationModule") or {}).get("nctId", ""),
                    "title": ((p.get("identificationModule") or {}).get("briefTitle") or "")[:120],
                    "status": status.get("overallStatus", ""), "phase": phase,
                    "posted": (status.get("lastUpdatePostDateStruct") or {}).get("date", ""),
                    "why_stopped": (status.get("whyStopped") or "")[:160],
                    "results": bool(s.get("hasResults")),
                    "sponsor": ((p.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name", "")})
    return [r for r in out if r["nct"]]


def changes(ticker: str, rows: list[dict], known: dict, today: date, first_look: bool) -> list[dict]:
    """What changed since the last look (`known[nct] = {status, results}`); updates `known`."""
    out = []
    fresh_cut = (today - timedelta(days=FRESH_DAYS)).isoformat()
    for r in rows:
        before = known.get(r["nct"])
        kind = None
        if r["status"] in STOPPED and (before is None or before["status"] != r["status"]):
            kind = "stopped"
        elif r["results"] and not (before or {}).get("results"):
            kind = "results"
        elif r["status"] in FINISHING and before is not None and before["status"] != r["status"]:
            kind = "completed" if r["status"] == "COMPLETED" else "enrolled"
        known[r["nct"]] = {"status": r["status"], "results": r["results"]}
        if not kind:
            continue
        if (first_look or before is None) and not (kind in ("stopped", "results") and r["posted"] >= fresh_cut):
            continue                                       # a first look learns; only fresh stops/results speak
        out.append({"ticker": ticker, "kind": kind, "was": (before or {}).get("status"), **r,
                    "seen": today.isoformat()})
    return out


def packet(companies: dict[str, str], state_path: Path | None = None, now: datetime | None = None,
           get=None) -> dict:
    """`companies` = {ticker: company name} for the drug makers held or considered.
    The brief's `trials`: feed health and the late-stage changes of the last week."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    get = get or _get_json
    state = {}
    if state_path is not None and Path(state_path).exists():
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    known = state.get("known") or {}
    looked = set(state.get("looked") or [])
    keep_cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    recent = [c for c in state.get("recent") or [] if c.get("seen", "") >= keep_cut]
    ok = errors = 0
    last_error = ""
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    for ticker, name in list(companies.items())[:MAX_COMPANIES]:
        sponsor = sponsor_name(name)
        if not sponsor:
            continue
        try:
            rows = studies(get(API.format(sponsor=quote(sponsor), since=since)))
            ok += 1
        except Exception as exc:
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        # the sponsor search is fuzzy: keep studies whose lead sponsor starts like the company
        head = sponsor.split()[0].lower()
        rows = [r for r in rows if r["sponsor"].lower().startswith(head)]
        new = changes(ticker, rows, known, today, first_look=ticker not in looked)
        looked.add(ticker)
        recent += [c for c in new if not any(c["nct"] == o["nct"] and c["kind"] == o["kind"] for o in recent)]
    if errors and not ok:
        blocked = any(x in last_error for x in ("403", "Forbidden", "Access Denied"))
        health = {"status": "blocked" if blocked else "broken", "why": last_error[:160]}
    else:
        health = {"status": "ok" if companies else "idle", "companies": ok, "errors": errors}
    if state_path is not None:
        try:
            Path(state_path).write_text(json.dumps({"known": known, "looked": sorted(looked),
                                                    "recent": recent[-60:]}), encoding="utf-8")
        except OSError as exc:
            log.warning("trials state not saved: %s", exc)
    return {"as_of": now.isoformat(timespec="minutes"), "source": health,
            "changes": sorted(recent, key=lambda c: (c["seen"], c["posted"]), reverse=True)[:12],
            "watching": sorted(companies)[:MAX_COMPANIES],
            "how": "Late-stage (Phase 2/3, 3) studies sponsored by these companies: stopped = suspended, terminated "
                   "or withdrawn; enrolled = finished recruiting (results to come); completed; results = results "
                   "posted. Context for IDEA lines (setup `trial`), never an order."}


def render(p: dict) -> str:
    lines = [f"CLINICAL TRIALS (feed {p['source']['status']}; watching {', '.join(p['watching']) or 'nobody'})"]
    if p["source"].get("why"):
        lines.append(f"  {p['source']['why']}")
    for c in p["changes"]:
        lines.append(f"  {c['ticker']} {c['kind'].upper()}: {c['title'][:80]} ({c['phase']}, {c['nct']}, posted "
                     f"{c['posted']})" + (f" why: {c['why_stopped'][:80]}" if c.get("why_stopped") else ""))
    if not p["changes"]:
        lines.append("  No late-stage changes in the last week.")
    return "\n".join(lines)
