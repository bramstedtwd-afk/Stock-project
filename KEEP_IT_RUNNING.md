# Keeping StockSage running for years

Three layers, each with one job:

| Layer | What it does | If it fails |
|---|---|---|
| **Always-on computer** | Reads your accounts, builds the sheet, serves your phone page, sends alerts | The watchdog tells your phone |
| **Cloud runner** | Learns and publishes market-side research with your computer off. Holds no broker login | Your computer still works alone |
| **Watchdog** (cloud) | Checks every weekday that the sheet is still being updated | You get a "has gone quiet" push |

## Part 1: set up the always-on computer (once, about 30 minutes)

This becomes THE machine. Do not leave scheduled jobs running on two computers:
you would get double alerts and two diverging copies of what StockSage learned.

1. **Install** Python 3.12 (python.org, tick "Add to PATH") and Git (git-scm.com).
2. **Get the code.** In PowerShell:
   ```
   cd C:\Users\<you>
   git clone https://github.com/bramstedtwd-afk/Stock-project.git
   cd Stock-project
   ```
3. **Bring over what it learned.** In your Google Drive's `StockSage` folder, download
   `brain-snapshot.db` into this computer's Downloads folder.
4. **Guided setup.**
   ```
   .\start.bat setup
   ```
   It finds the brain file, imports it, and runs the checks. When it asks you to link
   Robinhood, open the dashboard's Portfolio tab and type the login there yourself
   (never into a chat). Approve the sign-in on your phone.
5. **Turn everything on**:
   ```
   .\start.bat autopilot publish-drive
   .\start.bat autopilot
   .\start.bat mobile install
   .\start.bat alerts
   .\start.bat alerts test
   ```
   `alerts` prints a new topic: subscribe to it in the ntfy app (and delete the old one).
6. **Make it stay on.** Settings > System > Power & battery > Screen and sleep: set
   "When plugged in, put my device to sleep after" to **Never**. Set Windows Update
   "Active hours" to your waking hours. Lock the screen with Win+L but never sign out:
   the scheduled jobs run only while you are signed in. (If you want it to survive an
   update restart on its own, Windows can sign in automatically: run `netplwiz`. The
   trade-off is that anyone at the keyboard gets in.)
7. **Retire the old computer's jobs** (on the old one):
   ```
   .\start.bat autopilot off
   .\start.bat mobile off
   ```
   Then `.\start.bat doctor` on the new one. It should be all green.

## Part 2: add the watchdog (5 minutes, once)

1. On the always-on computer run `.\start.bat alerts` and copy the topic it prints.
2. GitHub: your repo > Settings > Secrets and variables > Actions > **New repository secret**.
   Name `STOCKSAGE_NTFY_TOPIC`, value the topic.
3. Actions tab > **Watchdog** > Run workflow. The log should say "The sheet is fresh."
4. Test it for real once: turn the always-on computer off for two weekdays and confirm
   you get "StockSage has gone quiet".

## Part 3: two traps that quietly kill things after months

- **GitHub switches scheduled jobs off after 60 days without any repository activity.**
  You get an email. Fix: Actions tab > pick the workflow > **Enable workflow**. A commit
  from you (even a small note) resets the clock. Put a reminder in your calendar every
  45 days until I make this automatic.
- **Google test-mode tokens expire in 7 days.** If the cloud runner worked at first and
  then fails weekly with `invalid_grant`, the Google "OAuth consent screen" is still in
  *Testing*. In Google Cloud Console set its publishing status to **In production**, then
  repeat the token step in SETUP.md.

## Part 4: the routine (what you do)

| When | Time | What |
|---|---|---|
| Every morning | 10 seconds | Read the 08:30 message. "All clear" means nothing to do. Open the phone page only if there is an action |
| Monthly | 2 minutes | `.\start.bat update`, then `.\start.bat doctor`. Fix anything yellow |
| Every quarter | 5 minutes | Read the scorecard at the bottom of the sheet. Re-run `.\start.bat research` |
| Every year | 30 minutes | Update Windows and Python, restart, run `doctor`. Change your Robinhood password and re-link. Confirm the Drive snapshot is recent. Decide whether any idea has earned trust (see STRATEGY.md) |

## Part 5: if the computer dies

On any new Windows computer repeat Part 1. The brain in Drive is refreshed on every
publish, so you lose at most a few hours of learning. The sheet history (the live
scorecard) travels in that brain too.

## Never change these

- StockSage never places or confirms an order. The agentic account's every order still
  needs your "confirm"; the other accounts you place yourself.
- Credentials live only in `.env` on the always-on computer. Not in chat, not in GitHub,
  not in Drive.
- The cloud jobs get no broker login, ever.
