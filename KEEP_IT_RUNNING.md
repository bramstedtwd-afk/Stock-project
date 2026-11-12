# Keeping StockSage running for years

Three layers, each with one job:

| Layer | What it does | If it fails |
|---|---|---|
| **Always-on computer** | Reads your accounts, builds the sheet, serves your phone page, sends alerts | The watchdog tells your phone |
| **Cloud runner** | Learns and publishes market-side research with your computer off. Holds no broker login | Your computer still works alone |
| **Watchdog** (cloud) | Checks every weekday that the sheet is still being updated | You get a "has gone quiet" push |

## Part 1: make THIS computer the always-on one (about 10 minutes)

StockSage is already installed here (`C:\Users\<you>\Stock-project`), so there is
nothing to clone, import or retire. Do not delete that folder: your `.env`
(Robinhood login, alert topic, phone key) lives in it.

1. **Update and check.** In PowerShell:
   ```
   cd C:\Users\<you>\Stock-project
   .\start.bat update
   .\start.bat doctor
   ```
   Fix anything marked `[!]` first (the `doctor` line tells you how).
2. **Turn everything on:**
   ```
   .\start.bat autopilot publish-drive
   .\start.bat autopilot
   .\start.bat mobile install
   .\start.bat alerts
   .\start.bat alerts test
   ```
   `alerts` prints a private topic. Subscribe to it in the ntfy app (a topic you may
   have made earlier is also fine: `alerts` keeps an existing one).
3. **Check it took:** `.\start.bat autopilot status` should show both the daily
   learning job and the publishing jobs as ON.
4. **Make it stay on.** Settings > System > Power & battery > Screen and sleep: set
   "When plugged in, put my device to sleep after" to **Never** (and hibernate to
   Never). On a laptop, also Control Panel > Power Options > "Choose what closing the
   lid does" > **Do nothing** when plugged in. Set Windows Update "Active hours" to
   your waking hours. Lock the screen with Win+L but never sign out: the scheduled
   jobs run only while you are signed in. (To survive an update restart by itself,
   Windows can sign in automatically: run `netplwiz`. The trade-off is that anyone at
   the keyboard gets in.)
5. **Leave it signed in and plugged in.** The first real test is tomorrow morning: you
   should get the 08:30 message, and the Drive file "StockSage Today" should show
   that day's time.

## Part 2: add the watchdog (5 minutes, once)

1. On the always-on computer run `.\start.bat alerts` and copy the topic it prints.
2. GitHub: your repo > Settings > Secrets and variables > Actions > **New repository secret**.
   Name `STOCKSAGE_NTFY_TOPIC`, value the topic.
3. Actions tab > **Watchdog** > Run workflow. The log should say "The sheet is fresh."
4. Test it for real once: turn the always-on computer off for two weekdays and confirm
   you get "StockSage has gone quiet".

## Part 3: two traps that quietly kill things after months

- **GitHub switches scheduled jobs off after 60 days without repository activity.**
  The Watchdog now re-enables both scheduled jobs every weekday, which resets that
  clock, so you should never need to. If you ever get GitHub's "scheduled workflows
  disabled" email anyway, open the Actions tab, pick the workflow, and click
  **Enable workflow**; then tell me, because the keep-alive step failed (its log line
  says which).
- **Google test-mode tokens expire in 7 days.** If the cloud runner worked at first and
  then fails weekly with `invalid_grant`, the Google "OAuth consent screen" is still in
  *Testing*. In Google Cloud Console set its publishing status to **In production**, then
  repeat the token step in SETUP.md.

## Part 4: the routine (what you do)

| When | Time | What |
|---|---|---|
| Every morning | 10 seconds | Read the 08:30 message. "All clear" means nothing to do. Open the phone page only if there is an action |
| Monthly | 2 minutes | `.\start.bat update` (unless you switched on self-update with `.\start.bat autopilot auto-update`), then `.\start.bat doctor`. Fix anything yellow |
| Every quarter | 5 minutes | Read the scorecard at the bottom of the sheet. Re-run `.\start.bat research` |
| Every year | 30 minutes | Update Windows and Python, restart, run `doctor`. Change your Robinhood password and re-link. Confirm the Drive snapshot is recent. Decide whether any idea has earned trust (see STRATEGY.md) |

## Part 5: if this computer dies or you replace it

On the new Windows computer:

1. Install Python 3.12 (python.org, tick "Add to PATH") and Git (git-scm.com).
2. `cd C:\Users\<you>` then `git clone https://github.com/bramstedtwd-afk/Stock-project.git`
   and `cd Stock-project`.
3. Download `brain-snapshot.db` from your Drive's `StockSage` folder into Downloads.
4. `.\start.bat setup`. It finds and imports the brain and runs the checks. Type your
   Robinhood login into the dashboard's Portfolio tab yourself (never into a chat) and
   approve the sign-in on your phone.
5. Repeat Part 1 steps 2 to 4 on the new computer. If the old one still works, run
   `.\start.bat autopilot off` and `.\start.bat mobile off` on it, so two computers do
   not run the same jobs.

The brain in Drive is refreshed on every publish, so you lose at most a few hours of
learning. The live scorecard travels in it too. Re-run Part 2 only if the alert topic
changed (update the `STOCKSAGE_NTFY_TOPIC` secret).

## Part 6: if this computer is off while the market is open (options, none built yet)

Written 2026-11-12 for you to choose from. Nothing here has been built or switched on.

**What already covers you today.** The trader still runs every hour from the cloud
(12:35-23:35 UTC) and checks every stop on each run, whether or not this computer is on.
What is lost while the computer is off is the minute-by-minute watch: a stop touched just
after a run waits up to an hour, in market hours. Your phone hears "the watcher has gone
quiet" after 150 quiet minutes and "the computer has gone quiet" after 30 business hours.
The phone page is also off, because this computer serves it.

| | Option | What you get | What it costs or risks |
|---|---|---|---|
| **A** | **Stop orders held at Robinhood for whole-share positions.** The trader places a resting stop order right after buying (the playbook already allows it), and prefers a whole share when the position size allows one. | Sells within seconds at the stop, with this computer off, the internet down, or Claude out of usage. No new machine, no cost. | Whole shares only: Robinhood does not hold stop orders for fractional shares, and in a small account most positions are fractional. Works in regular hours only. A stop order sells on a brief dip with no judgement, and after a gap down it sells at the open, wherever that is. The trader must cancel it before selling another way. |
| **B** | **Robinhood price alerts to your phone at each stop level,** kept up to date by the trader whenever it writes a STOP line. | Covers fractional shares too. Free. Reaches you with this computer off. | It tells you; it does not sell. You would open Robinhood and act. Needs your approval because the trader would create and delete alerts in your account. |
| **C** | **A second watcher on a small rented cloud computer** (about $5 a month), as a standby that only speaks when this computer's watcher has been silent for 10 minutes. | The full minute-by-minute watch survives a power cut or internet outage at home. | A monthly cost. The wake-up key and the Drive key would live on a rented machine (no broker login, ever). Needs building: the standby rule, so two watchers never wake the trader twice, plus a setup you would have to keep paid and updated. |
| **D** | **A second watcher on a spare device at home** (an old laptop or a small always-on box), same standby rule. | No monthly cost; the keys stay in your home. | A power cut or internet outage at home takes out both. Same building work as C. |
| **E** | **Tighten what exists:** every hourly run checks when the watcher last wrote, and if it has been silent for 20 minutes in market hours your phone is told on that run (today that takes 150 minutes). | No new machine, no keys anywhere new, about an hour to build. | Stops are still only checked hourly while the computer is off; you just learn about it sooner. |

**Recommendation:** E and A first (cheap, no new machine, no new keys), B if you want your
phone to hear a stop level being crossed. C or D only if this computer actually turns out to
go dark during market hours more than rarely; the scheduled cloud runs already check every
stop each hour. Not suitable: running the watcher on GitHub's free timers (they start hours
late, and the repository is public).

## Never change these

- StockSage never places or confirms an order. The agentic account's orders need your
  "confirm" unless YOU put `AUTONOMOUS MODE: ON for the agentic account.` in the trading
  routine's own instructions; then it trades only inside the limits in ROUTINE.md. To stop
  it at once, turn trade approvals ON in the Robinhood app. The other accounts you place
  yourself.
- Credentials live only in `.env` on the always-on computer. Not in chat, not in GitHub,
  not in Drive.
- The cloud jobs get no broker login, ever.
