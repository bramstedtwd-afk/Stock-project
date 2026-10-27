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

## Never change these

- StockSage never places or confirms an order. The agentic account's every order still
  needs your "confirm"; the other accounts you place yourself.
- Credentials live only in `.env` on the always-on computer. Not in chat, not in GitHub,
  not in Drive.
- The cloud jobs get no broker login, ever.
