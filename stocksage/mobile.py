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
a long random key (kept in .env, part of the link you scan). It serves exactly
one thing, only for GET, never a broker credential, never an action. It cannot
place or confirm an order, and it cannot trigger a broker read.
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


def render_page(now: float | None = None, path: Path | None = None) -> str:
    path = path or sheet_path()
    now = now or time.time()
    banner = ""
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
pre {{ white-space:pre-wrap; word-wrap:break-word; font:13.5px/1.5 ui-monospace, Menlo, Consolas, monospace; }}
</style></head><body><main>
<div class="stamp">StockSage &middot; {html.escape(stamp)} &middot; read-only</div>
{banner}<pre>{html.escape(text)}</pre>
<button onclick="location.reload()">Reload</button>
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
        if url.path != "/" or not key_ok(given, phone_key()):
            self._send(403, "Open the link StockSage printed for you (it includes a private key).")
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


def schtasks_create_cmd() -> list[str]:
    bat = PROJECT_ROOT / "start.bat"
    return ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON",
            "/TR", f'cmd /c start "" /min "{bat}" mobile serve']


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
        res = subprocess.run(schtasks_create_cmd(), capture_output=True, text=True)
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
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
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
