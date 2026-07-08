"""Run StockSage like a desktop app.

    python -m stocksage.desktop app       open in a native-feeling app window
    python -m stocksage.desktop web       open as a normal browser tab
    python -m stocksage.desktop phone     share to your phone over home Wi-Fi
    python -m stocksage.desktop install   put a StockSage icon on your desktop

App mode starts the Streamlit server in the background, opens a dedicated
chromeless window (Chrome/Edge/Brave/Chromium "--app" mode — Edge ships with
Windows, Chrome covers almost everyone else), and stops the server when the
window is closed. If no such browser exists it falls back to a normal tab.

`install` creates a real launcher: a StockSage.app bundle on macOS, a Desktop
shortcut on Windows, an applications-menu entry + desktop icon on Linux — so
after one `install` you never need a terminal again.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path("~/.stocksage").expanduser()
SERVER_LOG = STATE_DIR / "server.log"
DEFAULT_PORT = int(os.environ.get("STOCKSAGE_PORT", "8531"))
READY_TIMEOUT = 90  # seconds to wait for the server to come up


def say(msg: str) -> None:
    print(f"[stocksage] {msg}")


# --- server ------------------------------------------------------------------

def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_server(port: int, address: str = "localhost") -> subprocess.Popen | None:
    """Start Streamlit headless. Returns None if one is already running.

    `address` is the bind interface: localhost keeps StockSage private to
    this computer (app/web modes); 0.0.0.0 exposes it to your local network
    (phone mode).
    """
    if port_open(port):
        say("StockSage is already running — opening a window to it.")
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(SERVER_LOG, "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(PROJECT_ROOT / "app.py"),
            "--server.headless",
            "true",
            "--server.port",
            str(port),
            "--server.address",
            address,
            "--browser.gatherUsageStats",
            "false",
        ],
        cwd=PROJECT_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            say(f"The server exited unexpectedly. Log: {SERVER_LOG}")
            _print_log_tail()
            raise SystemExit(1)
        if port_open(port):
            return proc
        time.sleep(0.3)
    proc.terminate()
    say(f"The server did not become ready in {READY_TIMEOUT}s. Log: {SERVER_LOG}")
    _print_log_tail()
    raise SystemExit(1)


def _print_log_tail(lines: int = 15) -> None:
    try:
        for line in SERVER_LOG.read_text().splitlines()[-lines:]:
            print("   ", line)
    except OSError:
        pass


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# --- app window ----------------------------------------------------------------

_BROWSER_CANDIDATES_DARWIN = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
_BROWSER_CANDIDATES_WINDOWS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_BROWSER_NAMES_LINUX = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)


def find_app_browser() -> str | None:
    """A Chromium-family browser that supports chromeless --app windows."""
    if sys.platform == "darwin":
        candidates: tuple[str, ...] = _BROWSER_CANDIDATES_DARWIN
    elif sys.platform.startswith("win"):
        candidates = _BROWSER_CANDIDATES_WINDOWS
    else:
        return next((p for n in _BROWSER_NAMES_LINUX if (p := shutil.which(n))), None)
    return next((c for c in candidates if Path(c).exists()), None)


def open_app_window(url: str) -> subprocess.Popen | None:
    browser = find_app_browser()
    if browser is None:
        return None
    # A dedicated profile forces a separate browser process, so closing the
    # window ends this subprocess and we know when to stop the server.
    profile = STATE_DIR / "appwindow"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--window-size=1440,900",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --- modes ---------------------------------------------------------------------

def run_app(port: int = DEFAULT_PORT) -> int:
    url = f"http://localhost:{port}"
    server = start_server(port)
    window = open_app_window(url)
    if window is None:
        say("No Chrome/Edge-style browser found for app mode — using your browser instead.")
        return run_web(port, server)
    say("StockSage is open. Close the window to quit.")
    try:
        window.wait()
    except KeyboardInterrupt:
        window.terminate()
    finally:
        stop_server(server)
    return 0


_OWN_SERVER = object()  # sentinel: run_web should start (and own) the server


def run_web(port: int = DEFAULT_PORT, server=_OWN_SERVER) -> int:
    import webbrowser

    if server is _OWN_SERVER:
        server = start_server(port)
    url = f"http://localhost:{port}"
    webbrowser.open(url)
    say(f"StockSage is running at {url} — press Ctrl+C here to quit.")
    try:
        if server is not None:
            server.wait()
        else:  # attached to a server someone else started; just idle politely
            while port_open(port):
                time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(server)
    return 0


def lan_ip() -> str | None:
    """This computer's address on the local network (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _print_qr(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        say("(install the 'qrcode' package to get a scannable code here)")


def run_phone(port: int = DEFAULT_PORT) -> int:
    ip = lan_ip()
    if ip is None:
        say("Could not find this computer's network address — are you connected to Wi-Fi?")
        return 1
    server = start_server(port, address="0.0.0.0")
    url = f"http://{ip}:{port}"
    print()
    say(f"StockSage is live on your home network:  {url}")
    say("On your phone (same Wi-Fi): scan this code, or type the address in your browser.")
    print()
    _print_qr(url)
    print()
    say("Tip: use your phone browser's 'Add to Home Screen' to make it a phone app.")
    say("Note: while this runs, anyone on your Wi-Fi network can open the dashboard.")
    say("Press Ctrl+C here to stop sharing.")
    try:
        if server is not None:
            server.wait()
        else:
            while port_open(port):
                time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(server)
    return 0


# --- desktop icon installation ---------------------------------------------------

def install_icon() -> int:
    if sys.platform == "darwin":
        return _install_macos()
    if sys.platform.startswith("win"):
        return _install_windows()
    return _install_linux()


def _install_macos() -> int:
    app_root = Path("~/Applications/StockSage.app").expanduser()
    macos_dir = app_root / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    (app_root / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>CFBundleName</key><string>StockSage</string>\n"
        "  <key>CFBundleDisplayName</key><string>StockSage</string>\n"
        "  <key>CFBundleIdentifier</key><string>local.stocksage.app</string>\n"
        "  <key>CFBundleExecutable</key><string>stocksage</string>\n"
        "  <key>CFBundlePackageType</key><string>APPL</string>\n"
        "  <key>LSUIElement</key><false/>\n"
        "</dict></plist>\n"
    )
    launcher = macos_dir / "stocksage"
    launcher.write_text(f'#!/bin/bash\nexec "{PROJECT_ROOT}/start.sh" app\n')
    launcher.chmod(0o755)
    say(f"Installed {app_root}")
    say("Find StockSage in ~/Applications (or Spotlight) — drag it to your Dock.")
    return 0


def _install_windows() -> int:
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "[Environment]::GetFolderPath('Desktop') + '\\StockSage.lnk'); "
        f"$s.TargetPath = '{PROJECT_ROOT / 'start.bat'}'; "
        "$s.Arguments = 'app'; "
        f"$s.WorkingDirectory = '{PROJECT_ROOT}'; "
        "$s.WindowStyle = 7; "
        "$s.Description = 'StockSage — personal market intelligence'; "
        "$s.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True
    )
    if result.returncode != 0:
        say(f"Could not create the shortcut: {result.stderr.strip()}")
        return 1
    say("Installed a StockSage shortcut on your Desktop — double-click it to launch.")
    return 0


def _install_linux() -> int:
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=StockSage\n"
        "Comment=Personal market intelligence\n"
        f"Exec={PROJECT_ROOT}/start.sh app\n"
        f"Path={PROJECT_ROOT}\n"
        "Terminal=false\n"
        "Categories=Office;Finance;\n"
    )
    apps_dir = Path("~/.local/share/applications").expanduser()
    apps_dir.mkdir(parents=True, exist_ok=True)
    target = apps_dir / "stocksage.desktop"
    target.write_text(entry)
    target.chmod(0o755)
    desktop = Path("~/Desktop").expanduser()
    if desktop.is_dir():
        icon = desktop / "stocksage.desktop"
        icon.write_text(entry)
        icon.chmod(0o755)
        say(f"Installed {target} and a Desktop icon.")
    else:
        say(f"Installed {target} — StockSage now appears in your applications menu.")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .envfile import load_env

    load_env()
    mode = (argv or sys.argv[1:] or ["app"])[0]
    if mode == "app":
        return run_app()
    if mode == "web":
        return run_web()
    if mode == "phone":
        return run_phone()
    if mode == "install":
        return install_icon()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
