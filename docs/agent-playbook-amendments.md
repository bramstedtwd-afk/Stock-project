# Proposed Playbook v8 amendments

Paste these into **StockSage Routine Playbook.md** in the Drive `StockSage`
folder, replacing the corresponding v7 sections, and bump the version header
to v8.

> Edit the **existing** file in place. Do not create a second playbook
> document — a stray title match is what caused the v5/v6 loader bug, and
> path-based loading only protects you if exactly one playbook exists.

---

## A. Notification budget — one question a day, maximum

Every run ends in exactly one of three states. The default is silence.

1. **Nothing needed.** Write the log row. **Send no notification.**
   Explicitly includes: capital unchanged, positions unchanged, no stop
   within its warning band, health OK. A run that reaches the same
   conclusion as yesterday is not news.
2. **Watch item.** One line, no reply expected, **at most one per day**.
   Use when a position enters its warning band (cushion < 1.5%) or health
   degrades, but no decision is required yet.
3. **Decision needed.** One message, **one question**, a recommended
   default, and a deadline. Use for: a stop breach, a proposal ready to
   place, a held name entering its earnings blackout, or a health failure
   that invalidates the run.

A decision message must state the recommended action first and make the
reply trivial, e.g. *"GE breached its stop at $109.90. Recommend exiting at
the open. Reply HOLD by 9:15 ET to override."*

Rationale: between 7/25 and 8/10 the loop sent roughly forty notifications
whose entire actionable content was "add money." That volume trains the
owner to skim, which is precisely how the one message that matters gets
missed.

## B. Concentration is measured on equity, not buying power

Replace the v7 "max 50% per-ticker of buying power" rule.

- **Maximum 25% of total account equity in any one name.**
- **Minimum 4 positions** before any single name may exceed 15% of equity.
- **Existing positions count toward the cap.** Before proposing a buy,
  compute the post-trade weight:
  `(current position value + proposed dollars) / (total equity + proposed dollars)`
  and reject the proposal if it exceeds 25%.

Rationale: sizing off *buying power* sounds bounded but says nothing about
the resulting book. As of v42 the account is already **GE 37.5%** and
**MS 36.3%** of equity — 73.8% in two names — so a "50% of cash" rule would
have concentrated it further, not less. Both names are over the new cap
today: no new buys in either, and the owner should decide whether to trim.

## C. Split the risk check from the research sweep

Every run, unconditionally (cheap):
- positions cross-check, live quotes, stop cushions, breach detection
- brief `health` block

Only when settled buying power ≥ $1.00, **or** a position requires an exit:
- load the candidate list, run the scanner sweep, produce proposals

When capital-blocked, log one line — `capital-blocked, no proposals` — and
stop. Do not re-derive candidates that cannot be acted on. The risk half of
the run is what caught GE; the proposal half has produced nothing since 7/17.

## D. One rolling log, not one document per run

Replace the per-run Google Doc with a **single spreadsheet, one row per
run**, columns:

| run_utc | playbook_v | brief_as_of | health_ok | equity | settled_cash | positions | tightest_cushion | action | notified |
|---|---|---|---|---|---|---|---|---|---|

Keep a prose document only for runs where a decision was made or an anomaly
occurred. Forty-two separate documents is why a 16-day stale-research streak
went unnoticed by the owner despite being flagged in nine consecutive logs.

## E. Read the brief's health and headline first

The brief now carries two fields that lead every run:

- **`health.degraded`** — a list of plain-English failures. If non-empty,
  surface it **before** any suggestion. **Never propose a buy from a brief
  where `health.checks.earnings_calendar` is `false`** — the blackout is not
  protecting those names.
- **`headline`** — the model's edge over simply holding SPY. Quote this as
  the scoreboard. Do **not** headline raw hit rate, profit factor, or average
  return per call: those flatter the model in a rising market and are not
  evidence the tool beats an index fund.

The brief is now produced by `stocksage brief --out <path>`, so its schema is
versioned and tested in the repo rather than hand-rolled. Check
`schema_version` and warn if it is unfamiliar.
