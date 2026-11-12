"""The phone page: today's sheet, always on, one tap.

The full dashboard has to scan the market when it opens, so it is slow on a
phone and only exists while someone runs it. This is the opposite: a tiny
read-only page that serves the sheet the scheduled runs already saved
(`~/.stocksage/today.txt`), so it opens instantly and can start itself when the
computer starts.

  .\\start.bat mobile           show the link and QR code to scan once
  .\\start.bat mobile install   start the page automatically when you sign in
  .\\start.bat mobile off       stop starting it automatically

Safety: it is reachable by anything on your Wi-Fi, so every request must carry
a long random key (kept in .env, part of the link you scan). It serves the
saved sheet and, above it, the live overview (what the trader watcher saw on
its last check: each position's distance to its stop and target, today's move
against the market, ideas waiting for their entry, what is on the calendar;
the page re-reads that block every minute from `/live`, same key, plain text).
Only for GET, never a broker credential, never an action. It cannot place or
confirm an order, and it cannot trigger a broker read or a watcher check.
"""

from __future__ import annotations

import hmac
import html
import os
import secrets
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("STOCKSAGE_MOBILE_PORT", "8532"))
KEY_VAR = "STOCKSAGE_PHONE_KEY"
STALE_AFTER_HOURS = 30
TASK_NAME = "StockSage Phone"
LAUNCHD_LABEL = "local.stocksage.phone"
CRON_TAG = "# stocksage-phone"


def say(msg: str) -> None:
    print(f"[stocksage] {msg}")


# --- the key --------------------------------------------------------------------


def phone_key(create: bool = False) -> str | None:
    key = (os.environ.get(KEY_VAR) or "").strip()
    if key or not create:
        return key or None
    from . import envfile

    key = secrets.token_urlsafe(24)
    envfile.save_env({KEY_VAR: key})
    return key


def key_ok(given: str | None, expected: str | None) -> bool:
    """Constant-time check; no configured key means nobody gets in."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


# --- the page -------------------------------------------------------------------


def sheet_path() -> Path:
    from .today import state_dir

    return state_dir() / "today.txt"


WATCHER_LATE_MINUTES = 5        # the watcher checks every minute: this long without one is worth saying


def _pct_from(price: float, level: float) -> str:
    gap = level / price - 1
    return f"{abs(gap):.1%} {'above' if gap > 0 else 'below'}"


def live_text(now=None, folder: Path | None = None) -> str:
    """The live overview, as plain text: what the trader watcher saw on its last check
    (it writes its state file every minute). For each position: the price, today's move
    and the move against the market, and how far the stop and the target are. Then the
    ideas waiting for their entry price and what is on the calendar. Prices and percent
    only: no dollar amounts, no share counts, no account. Reads two local files; it
    cannot reach a broker or start a check. Empty when no watcher has run here."""
    import json
    from datetime import datetime, timezone

    from . import watcher

    folder = folder or sheet_path().parent
    now = now or datetime.now(timezone.utc)
    try:
        state = json.loads((folder / "watcher_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    checked = watcher._when(state.get("checked"))
    if checked is None:
        return ""
    try:
        memory = json.loads((folder / "watcher_memory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        memory = {}

    def clock(at):
        return at.astimezone(watcher.ET).strftime("%a %I:%M %p ET")

    ago = max(0, int((now - checked).total_seconds() // 60))
    lines = [f"LIVE  (the watcher's last check: {clock(checked)}, "
             + ("under a minute ago" if ago < 1 else f"{ago} min ago") + ")"]
    if ago >= WATCHER_LATE_MINUTES:
        lines.append(f"  THE WATCHER HAS NOT CHECKED FOR {ago} MINUTES. Is its window open on the computer? "
                     "On the computer: .\\start.bat watch-trader install")
    live = state.get("live") or {}
    spy = live.get("spy") or {}
    spy_at = watcher._when(spy.get("at"))
    spy_today = spy_at is not None and spy_at.astimezone(watcher.ET).date() == now.astimezone(watcher.ET).date()
    if spy_today:
        lines.append(f"  Market (SPY) {spy['change']:+.1%} today")
    held = watcher.holdings(memory)
    if not held:
        lines.append("  Nothing is held.")
    for h in held:
        t, row = h["ticker"], (live.get("rows") or {}).get(h["ticker"])
        at = watcher._when((row or {}).get("at"))
        if not row or not row.get("price") or at is None:
            lines.append(f"  {t}: no price yet")
            continue
        price, change = float(row["price"]), row.get("change")
        line = f"  {t}  {watcher._p(price)}"
        if change is not None and at.astimezone(watcher.ET).date() == now.astimezone(watcher.ET).date():
            line += f"  {change:+.1%} today"
            if spy_today and t not in watcher.FUNDS and not t.endswith("-USD"):
                line += f" ({change - spy['change']:+.1%} vs the market)"
        if (now - at).total_seconds() > 15 * 60:
            line += f"  [price from {clock(at)}]"
        lines.append(line)
        stop, target = h.get("stop"), h.get("target")
        if stop:
            touched = price <= stop
            detail = f"      stop {watcher._p(stop)} " + ("HAS BEEN TOUCHED" if touched else f"is {_pct_from(price, stop)}")
        else:
            detail = "      NO STOP ON RECORD"
        if target and target != float("inf"):
            detail += f"; target {watcher._p(target)} " + ("reached" if price >= target else f"is {_pct_from(price, target)}")
        lines.append(detail)
    ideas = state.get("idea_rows") or {}
    watched = {i["ticker"]: i for i in watcher.watched_ideas(memory, now, held)}
    if watched:
        lines += ["", "WAITING TO BUY  (good ideas not bought, watched for their entry price)"]
        for t, i in watched.items():
            row = ideas.get(t) or {}
            line = f"  {t}  [{i['level']}] {i.get('setup') or ''}: entry {watcher._p(i['entry'])}"
            if row.get("price"):
                line += f" is {_pct_from(float(row['price']), i['entry'])} (now {watcher._p(float(row['price']))})"
            if row.get("waiting"):
                line += f"; {row['waiting']}"
            lines.append(line)
    try:
        coming = watcher.upcoming(now, held, state.get("events") or {})
    except Exception:
        coming = []
    if coming:
        lines += ["", "COMING UP  (the trader is woken to review before each)"]
        lines += ["  " + r["text"].split(": decide")[0].split(": check")[0].replace("market: ", "") for r in coming]
    return "\n".join(lines)


def render_page(now: float | None = None, path: Path | None = None) -> str:
    path = path or sheet_path()
    now = now or time.time()
    banner = ""
    try:
        live = live_text(folder=path.parent)
    except Exception:                         # the overview must never take the sheet down with it
        live = ""
    live_block = (f'<pre id="live">{html.escape(live)}</pre>' if live else "")
    try:
        text = path.read_text(encoding="utf-8")
        age_h = (now - path.stat().st_mtime) / 3600.0
        when = time.strftime("%a %d %b, %H:%M", time.localtime(path.stat().st_mtime))
        if age_h > STALE_AFTER_HOURS:
            banner = (f'<p class="warn">This sheet is {age_h / 24:.0f} day(s) old. The computer has not '
                      "refreshed it: check that it is on and autopilot is running.</p>")
        stamp = f"Updated {when}"
    except OSError:
        text = ("No sheet yet.\n\nOn the computer run:\n    .\\start.bat today\n"
                "then reload this page.")
        stamp = "Nothing saved yet"
    trades, jump = "", ""
    try:                                     # the trader's record, if the trade memory has been built here
        report = (path.parent / "trades.txt").read_text(encoding="utf-8")
        trades = f'<h2 id="trades">Trades</h2><pre>{html.escape(report)}</pre>'
        jump = '<p><a href="#trades">Jump to the trader\'s grades and recent trades</a></p>'
    except OSError:
        pass
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="robots" content="noindex">
<title>StockSage</title>
<style>
:root {{ --bg:#fff; --fg:#14181f; --muted:#5b6575; --warn:#a15c00; --line:#d9dee7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1319; --fg:#e8ecf2; --muted:#9aa5b6; --warn:#f0b35a; --line:#2a3240; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }}
main {{ padding:12px 16px 48px; max-width:760px; margin:auto; }}
.stamp {{ color:var(--muted); font-size:13px; border-bottom:1px solid var(--line); padding-bottom:8px; }}
.warn {{ color:var(--warn); font-weight:600; }}
h2 {{ font-size:16px; margin:24px 0 4px; border-top:1px solid var(--line); padding-top:12px; }}
a {{ color:inherit; }}
pre {{ white-space:pre-wrap; word-wrap:break-word; font:13.5px/1.5 ui-monospace, Menlo, Consolas, monospace; }}
</style></head><body><main>
<div class="stamp">StockSage &middot; {html.escape(stamp)} &middot; read-only</div>
{jump}{banner}{live_block}<pre>{html.escape(text)}</pre>{trades}
<button onclick="location.reload()">Reload</button>
<script>
// Refresh the live overview each minute: a GET of plain text, shown as text (never as HTML).
setInterval(function () {{
  var box = document.getElementById("live");
  if (!box || document.hidden) return;
  fetch("/live" + location.search, {{cache: "no-store"}})
    .then(function (r) {{ return r.ok ? r.text() : null; }})
    .then(function (t) {{ if (t) box.textContent = t; }})
    .catch(function () {{}});
}}, 60000);
</script>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "StockSagePhone"

    def _send(self, code: int, body: str, kind: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        given = (parse_qs(url.query).get("k") or [None])[0]
        if url.path not in ("/", "/live") or not key_ok(given, phone_key()):
            self._send(403, "Open the link StockSage printed for you (it includes a private key).")
            return
        if url.path == "/live":                # the page's own once-a-minute refresh: plain text, same key
            try:
                self._send(200, live_text())
            except Exception:
                self._send(200, "")
            return
        self._send(200, render_page(), "text/html; charset=utf-8")

    do_HEAD = do_GET

    def _refuse(self) -> None:
        self._send(405, "Read-only.")

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    def log_message(self, *args) -> None:       # the key is in the URL: never write it to a log
        pass


def serve(port: int = PORT) -> int:
    phone_key(create=True)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError:
        say(f"Port {port} is already in use: the phone page is probably already running.")
        return 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# --- the link -------------------------------------------------------------------


def links(port: int = PORT) -> list[str]:
    from .desktop import lan_ip

    key = phone_key(create=True)
    hosts = []
    ip = lan_ip()
    if ip:
        hosts.append(ip)
    name = socket.gethostname()
    if name:
        hosts.append(name)
    return [f"http://{h}:{port}/?k={key}" for h in hosts]


def show_link() -> int:
    from .desktop import _print_qr

    found = links()
    if not found:
        say("Could not find this computer's network address: are you connected to Wi-Fi?")
        return 1
    print("\nYOUR PHONE PAGE (keep this link private: it is the password)")
    print("=" * 56)
    print(f"\n  {found[0]}\n")
    _print_qr(found[0])
    print("\n1. Phone on the same Wi-Fi: scan the code, or type the link.")
    print("2. Add it to your home screen (Share, then 'Add to Home Screen').")
    print("3. Make it start by itself:   .\\start.bat mobile install")
    if len(found) > 1:
        print(f"\nIf the address above stops working after a router restart, this name may still work:\n  {found[1]}")
    return 0


# --- starting by itself ---------------------------------------------------------


def start_command() -> list[str]:
    return [str(PROJECT_ROOT / ("start.bat" if sys.platform.startswith("win") else "start.sh")), "mobile", "serve"]


def startup_dir() -> Path:
    """The signed-in user's own Startup folder: anything here runs at sign-in,
    and writing to it needs no administrator rights (a sign-in scheduled task does)."""
    import os

    appdata = os.environ.get("APPDATA") or str(Path("~/AppData/Roaming").expanduser())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


STARTUP_FILE = "StockSage Phone.cmd"


def startup_script() -> str:
    bat = PROJECT_ROOT / "start.bat"
    return ("@echo off\r\n"
            "rem Starts the StockSage phone page at sign-in. Remove with: .\\start.bat mobile off\r\n"
            f'start "" /min "{bat}" mobile serve\r\n')


def launchd_plist() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>{PROJECT_ROOT}/start.sh</string><string>mobile</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
"""


def cron_line() -> str:
    return f'@reboot "{PROJECT_ROOT}/start.sh" mobile serve >/dev/null 2>&1 {CRON_TAG}'


def _plist_path() -> Path:
    return Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()


def install() -> int:
    phone_key(create=True)
    if sys.platform == "darwin":
        _plist_path().parent.mkdir(parents=True, exist_ok=True)
        _plist_path().write_text(launchd_plist(), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(_plist_path())], capture_output=True)
        res = subprocess.run(["launchctl", "load", str(_plist_path())], capture_output=True, text=True)
    elif sys.platform.startswith("win"):
        try:
            startup_dir().mkdir(parents=True, exist_ok=True)
            (startup_dir() / STARTUP_FILE).write_bytes(startup_script().encode("utf-8"))  # CRLF as written
            res = subprocess.CompletedProcess([], 0, "", "")
        except OSError as exc:
            res = subprocess.CompletedProcess([], 1, "", str(exc))
    else:
        from .autopilot import _current_crontab, _write_crontab

        keep = [ln for ln in _current_crontab().splitlines() if CRON_TAG not in ln]
        ok = _write_crontab("\n".join(keep + [cron_line()]) + "\n", fallback_lines=[cron_line()])
        res = subprocess.CompletedProcess([], 0 if ok else 1, "", "")
    if res.returncode != 0:
        say(f"Could not set it to start automatically: {(res.stderr or '').strip()}")
        return 1
    say("Your phone page will now start by itself when you sign in to this computer.")
    say("Starting it now too. Windows may ask to allow network access: allow it on private networks only.")
    try:
        subprocess.Popen(start_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return show_link()


def off() -> int:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", str(_plist_path())], capture_output=True)
        _plist_path().unlink(missing_ok=True)
    elif sys.platform.startswith("win"):
        (startup_dir() / STARTUP_FILE).unlink(missing_ok=True)
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)  # older installs
    else:
        from .autopilot import _current_crontab, _write_crontab

        keep = [ln for ln in _current_crontab().splitlines() if CRON_TAG not in ln]
        _write_crontab("\n".join(keep) + "\n" if keep else "")
    say("The phone page will no longer start by itself. (To stop one that is running now, restart the computer.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .envfile import load_env

    load_env()
    action = (argv if argv is not None else sys.argv[1:] or ["link"])
    action = action[0] if action else "link"
    return {"serve": serve, "install": install, "off": off, "link": show_link}.get(action, show_link)()


if __name__ == "__main__":
    raise SystemExit(main(["serve"]))
