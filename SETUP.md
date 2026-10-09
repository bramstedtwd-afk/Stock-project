# StockSage — setup & operations

Everything you need to install StockSage, move it between devices, run it
unattended, and fix it when something breaks. For what it *is* and how it
works, see [README.md](README.md).

---

## Quick start — one command

**Mac / Linux:**

```bash
./start.sh
```

**Windows:**

```bat
.\start.bat
```

That's it. The first run sets up everything automatically (virtual
environment, dependencies, settings file — allow a few minutes), then
**StockSage opens in its own app window** — no browser tabs, no terminal
juggling. Close the window and everything shuts down cleanly. Press
**Run daily cycle** to get your first suggestions, and link Robinhood right
from the **Portfolio tab** — no file editing needed.

The only prerequisite is [Python 3.10+](https://www.python.org/downloads/)
(on Windows, tick *"Add python.exe to PATH"* during install).

### Install it like a real app (recommended)

```bash
./start.sh install        # Windows: .\start.bat install
```

- **macOS** — creates **StockSage.app** in `~/Applications`: launch it from
  Spotlight or drag it to your Dock.
- **Windows** — puts a **StockSage** shortcut on your Desktop.
- **Linux** — adds StockSage to your applications menu and Desktop.

From then on it's double-click → app window. (The app window uses
Chrome/Edge/Brave under the hood; if none is installed it opens in your
default browser instead. `./start.sh web` forces browser mode.)

### Take it to any device — the brain travels with you

Everything StockSage has learned — signal weights, graded track record, move
memory — lives in **one file: the brain**. Three ways to move it, easiest
first:

**Shared brain across all your devices (set-and-forget):** open the
dashboard sidebar → **🧠 Brain → Share across your devices**. StockSage
detects your Dropbox / iCloud / OneDrive / Google Drive folder — pick it,
press **Share my brain**, done. Repeat on each device and they all read and
write the *same* brain — what one learns, all know. The sidebar always shows
where the brain lives, what it knows, and which device learned last. (Use
one device at a time; let the folder finish syncing before switching.)

Terminal equivalent:

```bash
./start.sh brain sync ~/Dropbox/StockSage     # or iCloud Drive / OneDrive / ...
```

**One-off transfer:** press **⬇️ Export brain** on the Learning tab (or
`./start.sh brain export`), move the file however you like, then **Import →
Merge** on the other device. Merging *compounds* knowledge — suggestions and
move history are unioned, and the most recently trained weights win — so
nothing is ever lost, no matter which direction you merge.

**New computer from scratch:**

```bash
git clone <your-repo-url> && cd Stock-project
./start.sh                        # sets itself up, opens the app
./start.sh brain import <file>    # or brain sync <folder>
```

**No admin rights / can't install Dropbox, iCloud, OneDrive, or Google
Drive for Desktop on this machine?** If you've already set up the [no-install
Google Drive API path](#the-learning-loop-the-point-of-the-whole-tool) (see
`publish-drive` below) on another device, the brain travels the exact same
way, with nothing to install here either:

```bash
git clone <your-repo-url> && cd Stock-project
./start.sh                        # sets itself up
./start.sh brain pull-drive       # pulls the whole shared brain straight from Drive
```

One-time OAuth consent (the same browser popup as `publish-drive`'s setup)
if this machine hasn't signed in before — after that it's one command, no
installer, ever, on this machine.

Robinhood credentials are deliberately **never** part of the brain — link
Robinhood fresh on each device. `./start.sh brain info` shows where the
brain lives and what it knows.

**Code improvements travel too:**

```bash
./start.sh update                 # Windows: .\start.bat update
```

Pull the latest StockSage code from your repository on any device — so when
we improve the tool on one machine (or merge a change on GitHub), every
other device catches up with one command. It's deliberately safe: it only
fast-forwards, refuses to touch uncommitted local edits, tells you exactly
what came in, and refreshes dependencies automatically when they changed.
Brain + code together mean a device is never more than two commands from
fully current: `./start.sh update` for the code, the shared brain (or
`brain import`) for the knowledge.

### Use it on your phone

```bash
./start.sh phone          # Windows: .\start.bat phone
```

This runs StockSage on your computer and shares it to your home Wi-Fi,
printing a **QR code** — scan it with your phone's camera and the dashboard
opens in your phone browser. Then use **Add to Home Screen** (Share menu on
iPhone, ⋮ menu on Android) and StockSage gets its own icon on your phone,
opening full-screen like a native app.

How it works and what to know:

- **Your credentials never leave your computer.** The engine (and your
  Robinhood link) runs on the computer; the phone is just a screen for it.
- The computer must be **on and running phone mode** while you use it, and
  the phone must be on the **same Wi-Fi**.
- While phone mode runs, anyone on your Wi-Fi network could open the
  dashboard — fine at home, skip it on public networks.
- Want it from anywhere (cellular, work, travel)? Install
  [Tailscale](https://tailscale.com) (free for personal use) on both your
  computer and phone, run phone mode, and use the computer's Tailscale
  address instead — a private encrypted tunnel, no ports exposed to the
  internet.

### Terminal mode

Anything you pass to the launcher goes to the CLI instead of the dashboard:

```bash
./start.sh daily          # the once-a-day heartbeat (learn -> scan -> suggest)
./start.sh suggest        # quick ranked scan (nothing recorded)
./start.sh suggest NVDA   # look at specific tickers
./start.sh sectors        # sector trend scoreboard
./start.sh portfolio      # your holdings + live signals
./start.sh moves          # "why it moved" memory
./start.sh performance    # learning status & signal weights
./start.sh profit         # paper ledger, incl. edge vs just holding SPY
./start.sh brief          # research packet for the trading routine
./start.sh publish <dir>  # publish brief + playbook + brain to a Drive folder
```

(Windows: `.\start.bat daily`, etc.)


---

## Running it unattended (autopilot)

The dashboard runs the learn+scan cycle the first time you open it each day.
To have it learn without you, schedule it with your operating system — it
then runs every weekday at 5:30pm even when nothing is open:

```bash
./start.sh autopilot                       # learn every weekday 17:30
./start.sh autopilot publish "<folder>"    # + supply the trading routine
./start.sh autopilot status                # check both
./start.sh autopilot off                   # stop everything
```

Uses launchd on macOS, cron on Linux, Task Scheduler on Windows; output
goes to `~/.stocksage/daily.log` and `~/.stocksage/publish.log`. The
computer must be awake at run time.

**`autopilot publish "<folder>"`** is the hands-off trading bridge: every
weekday (5 times, timed just ahead of a typical trading routine's runs)
it mirrors your Robinhood fills into graded calls, grades matured ones,
and writes a fresh research brief into a Google-Drive-synced folder — so
your trading routine reads freshly-graded research on every run with
zero manual steps. Point `<folder>` at a directory synced by Google
Drive for Desktop (e.g. `"G:\My Drive\StockSage"`).

**No admin rights / can't install Google Drive for Desktop?** Use
`./start.sh autopilot publish-drive` instead — it talks to Google
Drive's API directly from Python, with nothing installed beyond two
packages already in StockSage's own virtual environment. One-time
setup (all in a browser, no downloads):

1. Go to [console.cloud.google.com](https://console.cloud.google.com/),
   create a project (free), then **APIs & Services → Library** →
   enable the **Google Drive API**.
2. **APIs & Services → OAuth consent screen** → External → fill in an
   app name and your email → save (you can leave it in "Testing" mode).
3. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** → Application type: **Desktop app** → Create.
4. Click the download icon next to the new client → save the file as
   `drive_credentials.json` in your `~/.stocksage/` folder (Windows:
   `C:\Users\<you>\.stocksage\drive_credentials.json` — create the
   folder if it doesn't exist).
5. Run `./start.sh publish-drive` once by hand — it opens your browser
   for a one-time "Sign in with Google" consent, then never asks again.

From then on `./start.sh autopilot publish-drive` schedules the exact
same capture → grade → publish cycle, just delivered straight to Drive's
API instead of a synced folder. Everything lands in one "StockSage"
folder in your Drive, in three files that get updated in place each
run (no dated duplicates to clean up, no ambiguity about which is
current).

**Running independent of any personal device (no computer needs to be
on):** `publish-drive` has no dependency on a locally-mounted folder, so
it can run from any machine that can reach the internet — including a
scheduled cloud session (a Claude Code Routine, a CI runner, any
headless box) that has no browser and no local `~/.stocksage/` history
of its own. Two things make that work, both already built in:

- **No browser needed there.** After the one-time browser consent above
  on any device, that device's `~/.stocksage/drive_token.json` can be
  carried to the headless machine in a `STOCKSAGE_DRIVE_TOKEN`
  environment variable; `get_service()` seeds the token file from it on
  first use and refreshes silently afterwards, so
  `drive_credentials.json` is never needed there.

  **Understand what you are trading away before doing this.** That value
  is a live OAuth refresh token — a durable credential, not a session —
  and most cloud-session platforms have no secrets store, so environment
  variables there are readable by anyone who can edit the environment.
  Two things bound the damage: the token's `drive.file` scope means it
  can only ever touch files this app created, never the rest of your
  Drive, and it can be revoked instantly at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
  Prefer a real secrets store where the platform offers one. If you use
  the env-var path, treat the value like a password and revoke it the
  moment the machine is no longer yours.

- **No local brain history needed there either.** Every `publish-drive`
  run first calls `brain pull-drive` internally — pulling whatever the
  brain last learned anywhere (this device, another device, an earlier
  cloud run) from Drive and merging it in before grading or scanning —
  then re-publishes the merged, newly-updated brain back to Drive when
  it's done. A totally fresh machine with an empty database starts smart
  on its very first run, and every run anywhere keeps the one shared
  brain moving forward together.

Robinhood credentials are **not** required on a headless publish-drive
machine — without them it just degrades to percentage-based position
sizing in the brief (the trading routine already knows to compute a
dollar amount itself from its own live buying power when that happens;
see `ROUTINE.md`). Only the Drive token needs to travel.

---

## Your daily "what do I do" sheet

```
cd C:\Users\<you>\Stock-project
.\start.bat analogs      (once: builds the look-alike history, a few minutes)
.\start.bat today        (the sheet: SELL / TRIM / BUY per account)
```

It says SELL and TRIM plainly (stops, size cap and the 10-day clock are rules).
It says BUY plainly **only** when look-alike history backs that call; otherwise
the name is listed as an idea, not an instruction. News and sector trends are
shown under each call for you to weigh, but they never change it. It also shows
at the top of the dashboard, and each scheduled run on your computer saves it
and puts a copy in your Drive folder as **StockSage Today** so you can read it on
your phone. StockSage never places an order; you do that yourself.

## Your phone page (always on, opens instantly)

```
cd C:\Users\<you>\Stock-project
.\start.bat mobile install
```

Scan the code once, add it to your home screen, and from then on it is one tap.
It starts by itself when you sign in to the computer and shows today's sheet as
last saved by a scheduled run (the computer must be on and on your Wi-Fi). The
link contains a private key: treat it like a password. `.\start.bat mobile`
shows the link again; `.\start.bat mobile off` stops it starting.

## Phone alerts

```
cd C:\Users\<you>\Stock-project
.\start.bat alerts          (makes a private topic name and tells you what to type in the app)
.\start.bat alerts test     (sends a test to your phone)
.\start.bat alerts off
```

Install the free **ntfy** app, subscribe to the topic it prints, and you get a loud
buzz when a stop is breached and one quiet daily message with everything else.
Messages carry a ticker, an action and the account type only, never an amount.

## If anything seems off

```bash
./start.sh doctor             # Windows: .\start.bat doctor
```

Checks everything that can go wrong — Python, dependencies, settings,
brain integrity, market-data access, Robinhood login, update channel,
autopilot — and prints a plain-language fix for anything that isn't right.
Safe to run anytime; changes nothing.


## Account security

```bash
./start.sh security              # Windows: .\start.bat security
./start.sh security --signin 8:30am
```

Reports how exposed your stored broker login is, lists every Robinhood
session StockSage has opened (marking a **fresh sign-in**, which Robinhood
alerts you about, apart from a **reused token**, which is silent), and
tells you how to check the same thing from Robinhood's own device list.
`--signin` answers a specific alert directly.

See the Security section of the README for what is stored where.

---

## Run it in the cloud (no computer needs to be on)

The five scheduled Windows tasks only work while that PC is awake and signed in.
A GitHub Action can do the market-side work instead: scan, grade, learn, and
publish the brief and brain to your Drive. It runs on a schedule, on weekdays,
with nothing of yours switched on.

**What it does not do.** It has no Robinhood access, on purpose: this repository
and its workflow logs are public, and a broker password has no place in either.
So the cloud handles the research. Which account holds what is still worked out
on your own machine and by your routine, where the access is legitimate.

### One-time setup (about 5 minutes)

1. **Copy your Drive token without showing it.** In PowerShell on the machine
   where `publish-drive` already works:
   ```
   Get-Content $env:USERPROFILE\.stocksage\drive_token.json -Raw | Set-Clipboard
   ```
   Nothing prints. The token is now on your clipboard. Do not paste it anywhere
   except the next step, and never into a chat.

2. **Store it as a secret.** On GitHub: your repository -> **Settings** ->
   **Secrets and variables** -> **Actions** -> **New repository secret**.
   Name it exactly `STOCKSAGE_DRIVE_TOKEN`, paste, save. GitHub encrypts it and
   masks it in logs.

3. **Run it once by hand.** **Actions** tab -> **Cloud research** -> **Run
   workflow**. Open the run and read the step "Can this runner reach market
   data?". Green checks beside *Market data* mean it works. If it says Yahoo is
   unreachable, cloud addresses are being throttled and the cloud runner will
   not be reliable; keep the Windows tasks and tell me.

4. **Watch it for two weekdays**, then turn the Windows tasks off so two machines
   are not learning at once:
   ```
   cd C:\Users\<you>\Stock-project
   .\start.bat autopilot off
   ```

### Optional: get told when it goes quiet

Make a free check at healthchecks.io set to expect a ping about every 4 hours on
weekdays, and store its URL as a second secret named `STOCKSAGE_HEALTHCHECK_URL`.
The workflow pings it after every good run. If runs stop, you get the email.

### Things worth knowing

- **Times shift by an hour in November.** The schedule is in UTC and was set for
  Central *daylight* time. After the clocks change, runs land an hour earlier on
  the wall clock. Edit the cron lines in `.github/workflows/cloud-research.yml`.
- **GitHub can run a scheduled job several minutes late** under load, and
  occasionally skips one. That is why the brief carries its own timestamp.
- **GitHub pauses scheduled jobs on a public repo after 60 days with no
  activity.** Any commit resets it.
- **Logs are public.** They contain market research only. They cannot contain your
  holdings or balances, because the cloud never has access to your accounts.
- **Use one learner at a time.** If both the cloud and a PC run the daily learning,
  the later one's weights win a merge. Turn the Windows autopilot off once the
  cloud run is healthy.
