"""StockSage dashboard — run with:  streamlit run app.py

A single-user cockpit over the engine: today's suggestions, sector trends,
your Robinhood portfolio, the move-context memory, and the learning status.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from stocksage.db import Database
from stocksage.engine import Engine
from stocksage.envfile import load_env, save_env
from stocksage.robinhood import RobinhoodClient

load_env()  # pick up .env automatically; real environment still wins

st.set_page_config(page_title="StockSage", page_icon="📈", layout="wide")

# A damaged brain (e.g. a partial cloud-sync write) must degrade to
# instructions, not a stack trace.
import sqlite3  # noqa: E402

try:
    _probe = Database()
    _probe.conn.execute("SELECT 1 FROM meta LIMIT 1")
    _brain_path = _probe.path
    _probe.close()
except sqlite3.DatabaseError:
    st.error(
        "Your brain file appears damaged (this can happen if a cloud-sync "
        "was interrupted mid-write). To recover:\n\n"
        "1. If you use a shared brain, wait for your cloud folder to finish "
        "syncing, then reload this page.\n"
        "2. If you have a brain export, restore it: "
        "`./start.sh brain import <file> --replace`\n"
        "3. Otherwise, delete the brain file and StockSage will rebuild "
        "from two years of history on the next run."
    )
    st.stop()

ACTION_COLORS = {
    "STRONG BUY": "#0a7a3d",
    "BUY": "#3ba55d",
    "HOLD": "#8a8f98",
    "SELL": "#d0763b",
    "STRONG SELL": "#c0392b",
}


@st.cache_resource
def get_engine() -> Engine:
    return Engine()


def run_daily_cycle():
    engine = get_engine()
    first_run = engine.db.get_meta("bootstrap_done") is None
    label = (
        "First run: learning from two years of history, then scanning the universe "
        "(a few minutes)..."
        if first_run
        else "Evaluating matured calls, learning, scanning the universe..."
    )
    try:
        with st.spinner(label):
            result = engine.daily_run()
    except Exception:
        import traceback

        st.error(
            "The daily cycle hit a problem — usually a network hiccup fetching "
            "market data. It's safe to press **Run daily cycle** to retry. "
            "If it keeps happening, run `./start.sh doctor` in a terminal for "
            "a full diagnosis."
        )
        with st.expander("Technical details"):
            st.code(traceback.format_exc())
        return
    st.session_state["scan_result"] = result
    if result.errors and len(result.errors) > len(result.suggestions):
        st.warning(
            f"Market data was unavailable for {len(result.errors)} of the names "
            "scanned — results may be thin. Usually temporary; retry in a minute."
        )


st.title("📈 StockSage")
st.caption(
    "Your personal learning market engine. Decision support only — every trade is your call."
)

# --- Sidebar: the brain, always visible ---------------------------------------
with st.sidebar:
    from stocksage.brain import brain_info, detect_cloud_folders, sync_to_folder

    st.header("🧠 Brain")
    binfo = brain_info()
    if binfo["shared"]:
        st.success("☁️ Shared across your devices")
        st.caption(f"Lives in: `{binfo['path']}`")
    else:
        st.info("💻 On this device only")
    st.caption(
        f"{binfo['suggestions']} calls on record · {binfo['move_events']} moves "
        f"remembered · {binfo['warmup_samples']} history samples"
    )
    if binfo["last_device"]:
        st.caption(f"Last learned on **{binfo['last_device']}** ({binfo['last_device_at']})")

    st.header("⭐ Watchlist")
    st.caption("Names beyond the built-in universe you want scanned daily. "
               "Anything you hold on Robinhood is always included automatically.")
    watch_db = Database()
    current_watch = watch_db.watchlist()
    if current_watch:
        for w in current_watch:
            wc1, wc2 = st.columns([3, 1])
            wc1.write(w)
            if wc2.button("✕", key=f"unwatch-{w}", help=f"Stop watching {w}"):
                watch_db.watchlist_remove(w)
                st.rerun()
    new_watch = st.text_input("Add ticker", placeholder="e.g. PLTR", key="watch_input")
    if st.button("Add to watchlist", use_container_width=True) and new_watch.strip():
        watch_db.watchlist_add(new_watch)
        st.rerun()

    if not binfo["shared"]:
        with st.expander("☁️ Share across your devices"):
            st.caption(
                "Puts the brain in a folder your cloud drive syncs. Run the same "
                "thing on your other devices and they all share one mind. "
                "Use one device at a time."
            )
            detected = detect_cloud_folders()
            options = [f"{name}  ({path})" for name, path in detected] + ["Other folder…"]
            choice = st.selectbox("Where?", options)
            if choice == "Other folder…":
                custom = st.text_input("Folder path (must exist)")
                target_dir = Path(custom).expanduser() if custom else None
            else:
                target_dir = detected[options.index(choice)][1] / "StockSage"
            if st.button("Share my brain", type="primary", use_container_width=True):
                if target_dir is None or not target_dir.parent.exists():
                    st.error("Pick or enter an existing folder first.")
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    new_home = sync_to_folder(target_dir)
                    st.cache_resource.clear()  # engine must reopen the moved brain
                    st.success(f"Done — the brain now lives in {new_home}")
                    st.rerun()

# Continual learning: if today's cycle hasn't happened yet, run it on open.
if "scan_result" not in st.session_state and not st.session_state.get("autorun_done"):
    st.session_state["autorun_done"] = True
    from datetime import date as _date

    if get_engine().db.get_meta("last_daily_run") != _date.today().isoformat():
        run_daily_cycle()

col_btn, col_status = st.columns([1, 4])
with col_btn:
    if st.button("🔄 Run daily cycle", type="primary", use_container_width=True):
        run_daily_cycle()

result = st.session_state.get("scan_result")

if result is not None:
    from stocksage.briefing import briefing_lines, build_briefing

    briefing = build_briefing(result, get_engine().db)
    mood_icon = {"bullish": "🟢", "bearish": "🔴", "mixed": "🟡"}[briefing["mood"]]
    with st.container(border=True):
        st.markdown(f"### ☀️ Today's briefing {mood_icon}")
        for line in briefing_lines(briefing):
            st.markdown(f"- {line}")

with col_status:
    if result is None:
        st.info(
            "Already learned today — press **Run daily cycle** for a fresh scan anyway."
        )
    else:
        linked = "linked ✅" if result.portfolio else "not linked"
        bootstrap_note = ""
        if result.bootstrap_stats:
            bs = result.bootstrap_stats
            bootstrap_note = (
                f" · bootstrapped from history: {bs['warmup_samples']} training samples, "
                f"{bs['move_events_backfilled']} past moves remembered"
            )
        st.success(
            f"Scanned {len(result.suggestions)} names · graded {result.evaluated_count} "
            f"matured suggestions · Robinhood {linked}{bootstrap_note}"
        )

tab_sugg, tab_profit, tab_sectors, tab_portfolio, tab_moves, tab_learning = st.tabs(
    ["💡 Suggestions", "💰 Profit", "🏭 Sectors", "💼 Portfolio", "📰 Why it moved", "🧠 Learning"]
)

with tab_sugg:
    if result is None:
        st.write("Run the daily cycle first.")
    else:
        show_holds = st.toggle("Show HOLDs", value=False)
        pool = result.suggestions if show_holds else result.actionable
        if not pool:
            st.write("No actionable suggestions today — that is a valid answer too.")
        for s in pool:
            color = ACTION_COLORS.get(s.action, "#8a8f98")
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2, 2, 2, 4])
                c1.markdown(f"### {s.ticker}")
                c1.caption(s.sector or "")
                c2.markdown(
                    f"<span style='background:{color};color:white;padding:4px 10px;"
                    f"border-radius:6px;font-weight:600'>{s.action}</span>",
                    unsafe_allow_html=True,
                )
                c2.metric("Score", f"{s.risk_adjusted_score:+.2f}")
                c3.metric("Price", f"${s.price:,.2f}")
                if s.position_fraction:
                    c3.caption(f"Suggested size: {s.position_fraction:.0%} of cash")
                if s.owned_shares:
                    c3.caption(f"You hold {s.owned_shares:g} shares")
                with c4:
                    for note in s.notes:
                        st.caption(f"· {note}")
                    with st.expander("Signal breakdown"):
                        sig_df = pd.DataFrame(
                            sorted(s.signals.items(), key=lambda kv: -abs(kv[1])),
                            columns=["signal", "value"],
                        )
                        st.dataframe(sig_df, hide_index=True, use_container_width=True)

with tab_profit:
    from stocksage.profit import default_stake, paper_trades, profit_stats

    db = Database()
    buys, avoided = paper_trades(db.evaluated_suggestions())
    stats = profit_stats(buys, avoided)
    stake = default_stake()
    st.caption(
        f"The honest meter: every graded call scored as a ${stake:,.0f} paper trade. "
        "This is what following StockSage would have earned — judge it here before "
        "trusting it with real size."
    )
    if not buys and not avoided:
        st.info(
            "The ledger fills in as suggestions mature (5 trading days each). "
            "Check back after the first week of daily cycles."
        )
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Paper P&L", f"${stats['total_pnl']:+,.2f}")
        m2.metric(
            "Win rate",
            f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "—",
            help="Share of buy-side calls that made money over their 5-day horizon.",
        )
        m3.metric(
            "Profit factor",
            f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "—",
            help="Gross wins ÷ gross losses. Above 1.0 means the wins pay for the losses.",
        )
        m4.metric(
            "Risk avoided",
            f"${stats['risk_avoided']:+,.2f}",
            help="What the sell/avoid calls saved you by being out of falling names.",
        )
        if buys:
            curve = pd.DataFrame(
                {"Date": [t.when for t in buys], "Cumulative P&L ($)": [t.cumulative for t in buys]}
            ).groupby("Date").last()
            st.line_chart(curve)
            if stats["best"] and stats["worst"]:
                c1, c2 = st.columns(2)
                b, w = stats["best"], stats["worst"]
                c1.caption(f"🏆 Best call: **{b.ticker}** {b.when} → ${b.pnl:+,.2f}")
                c2.caption(f"💥 Worst call: **{w.ticker}** {w.when} → ${w.pnl:+,.2f}")
            with st.expander("Every paper trade"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Date": t.when,
                                "Ticker": t.ticker,
                                "Action": t.action,
                                "Return %": round(t.realized_return * 100, 2),
                                "P&L $": round(t.pnl, 2),
                                "Cumulative $": round(t.cumulative, 2),
                            }
                            for t in reversed(buys)
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                )

with tab_sectors:
    if result is None or not result.sector_trends:
        st.write("Run the daily cycle first.")
    else:
        trend_df = (
            pd.DataFrame(
                sorted(result.sector_trends.items(), key=lambda kv: -kv[1]),
                columns=["Sector", "Trend score"],
            )
            .set_index("Sector")
        )
        st.bar_chart(trend_df, horizontal=True)
        st.caption("Composite trend score per sector ETF: -1 bearish … +1 bullish.")

with tab_portfolio:
    if result is not None and result.portfolio:
        p = result.portfolio
        m1, m2, m3 = st.columns(3)
        m1.metric("Holdings", len(p.holdings))
        m2.metric("Equity", f"${p.total_equity:,.2f}")
        m3.metric("Buying power", f"${p.buying_power:,.2f}")
        by_ticker = {s.ticker: s for s in result.suggestions}
        rows = []
        for h in p.holdings:
            sig = by_ticker.get(h.ticker)
            pl = (h.current_price / h.avg_buy_price - 1.0) if h.avg_buy_price else None
            rows.append(
                {
                    "Ticker": h.ticker,
                    "Shares": h.shares,
                    "Avg cost": h.avg_buy_price,
                    "Price": h.current_price,
                    "P/L %": round(pl * 100, 1) if pl is not None else None,
                    "Signal": sig.action if sig else "n/a",
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        # --- background insight from your full trading history ---
        from stocksage.insights import model_alignment, trading_insights

        hist_db = Database()
        orders = hist_db.rh_orders()
        if result.rh_sync:
            st.caption(
                f"History synced: {result.rh_sync['orders_total']} orders mirrored "
                f"({result.rh_sync['orders_added']} new this run)."
            )
        if orders:
            st.divider()
            st.subheader("📜 What your history says")
            ti = trading_insights(orders, hist_db.rh_dividends())
            i1, i2, i3, i4 = st.columns(4)
            i1.metric("Realized P&L", f"${ti['realized_pnl']:+,.2f}",
                      help="FIFO-matched, across your whole Robinhood history.")
            i2.metric("Your win rate",
                      f"{ti['win_rate']:.0%}" if ti["win_rate"] is not None else "—",
                      help=f"Across {ti['round_trips']} completed round trips.")
            i3.metric("Avg holding time",
                      f"{ti['avg_held_days']:.0f} days" if ti["avg_held_days"] is not None else "—")
            i4.metric("Dividends collected", f"${ti['dividends_total']:,.2f}")
            if ti["best_name"] and ti["worst_name"]:
                c1, c2 = st.columns(2)
                c1.caption(f"🏆 Best name for you: **{ti['best_name'][0]}** "
                           f"(${ti['best_name'][1]:+,.2f} realized)")
                c2.caption(f"💥 Costliest: **{ti['worst_name'][0]}** "
                           f"(${ti['worst_name'][1]:+,.2f} realized)")

            align = model_alignment(orders, hist_db.recent_suggestions(1000))
            if align["agreement_rate"] is not None:
                st.caption(
                    f"🤝 Your trades agreed with the model "
                    f"{align['agreement_rate']:.0%} of the time "
                    f"({align['agreed']} agreed, {align['disagreed']} disagreed, "
                    f"{align['uncovered']} with no standing call)."
                )
                if align["disagreements"]:
                    with st.expander("Where you and the model disagreed"):
                        st.dataframe(
                            pd.DataFrame(align["disagreements"]),
                            hide_index=True, use_container_width=True,
                        )
            with st.expander("Completed round trips (FIFO)"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"Ticker": t.ticker, "Qty": t.quantity,
                             "Bought": t.buy_price, "Sold": t.sell_price,
                             "P&L $": round(t.pnl, 2), "Held (days)": t.held_days,
                             "Opened": t.opened, "Closed": t.closed}
                            for t in reversed(ti["trips"])
                        ]
                    ),
                    hide_index=True, use_container_width=True,
                )
        else:
            st.caption(
                "Trading-history insights appear after the first daily cycle "
                "mirrors your Robinhood order history."
            )
    elif RobinhoodClient.credentials_available():
        st.write("Run the daily cycle to pull your portfolio.")
    else:
        st.info(
            "Robinhood is not linked yet. Enter your login below — it is saved "
            "only to a private `.env` file on this machine and used read-only "
            "(StockSage never places orders)."
        )
        with st.form("link_robinhood"):
            username = st.text_input("Robinhood email")
            password = st.text_input("Robinhood password", type="password")
            mfa = st.text_input(
                "Authenticator (TOTP) secret — optional",
                type="password",
                help=(
                    "Only needed if you use app-based two-factor auth: paste the "
                    "setup key Robinhood showed when you configured your "
                    "authenticator app."
                ),
            )
            if st.form_submit_button("🔗 Link Robinhood", type="primary"):
                if not username or not password:
                    st.error("Email and password are both required.")
                else:
                    save_env(
                        {
                            "ROBINHOOD_USERNAME": username,
                            "ROBINHOOD_PASSWORD": password,
                            "ROBINHOOD_MFA_SECRET": mfa or None,
                        }
                    )
                    st.success("Saved. Run the daily cycle to pull your portfolio.")
                    st.rerun()

with tab_moves:
    db = Database()
    ticker_filter = st.text_input("Filter by ticker (blank = all)").strip().upper() or None
    events = db.move_events(ticker=ticker_filter, limit=60)
    if not events:
        st.write("No significant moves recorded yet — the memory builds with each daily run.")
    for row in events:
        reasons = json.loads(row["reasons"]) or ["unexplained"]
        direction = "🟢" if row["return_pct"] > 0 else "🔴"
        with st.expander(
            f"{direction} {row['event_date']} — **{row['ticker']}** "
            f"{row['return_pct'] * 100:+.1f}%  ·  {', '.join(reasons)}"
        ):
            for h in json.loads(row["headlines"]):
                link = h.get("link") or ""
                title = h.get("title", "")
                pub = h.get("publisher", "")
                st.markdown(f"- [{title}]({link}) — {pub}" if link else f"- {title} — {pub}")

with tab_learning:
    db = Database()
    summary = db.performance_summary()
    weights = db.load_weights()
    m0, m1, m2, m3 = st.columns(4)
    m0.metric("Historical samples", db.get_meta("warmup_samples") or "0")
    m1.metric("Suggestions graded", summary["evaluated"])
    m2.metric(
        "Direction hit rate",
        f"{summary['hit_rate']:.0%}" if summary["hit_rate"] is not None else "—",
    )
    m3.metric(
        "Avg realized return",
        f"{summary['avg_return'] * 100:+.2f}%" if summary["avg_return"] is not None else "—",
    )
    if weights:
        st.subheader("Learned signal weights")
        w_df = (
            pd.DataFrame(sorted(weights.items(), key=lambda kv: -kv[1]), columns=["Signal", "Weight"])
            .set_index("Signal")
        )
        st.bar_chart(w_df, horizontal=True)
        st.caption(
            "Signals that keep calling direction correctly earn weight; ones that miss lose it. "
            "A floor keeps every signal alive so the model can re-adapt when regimes change."
        )
        sector_grades = db.sector_grade_counts()
        if sector_grades:
            from stocksage.learning import MIN_SECTOR_GRADES

            st.subheader("Per-sector learning")
            st.caption(
                "Each sector also learns its own weights (what works in Energy isn't "
                f"what works in Tech). A sector's weights start voting after "
                f"{MIN_SECTOR_GRADES} graded calls."
            )
            sector_df = pd.DataFrame(
                [
                    {
                        "Sector": name,
                        "Graded calls": n,
                        "Status": "✅ voting" if n >= MIN_SECTOR_GRADES else "🌱 learning",
                    }
                    for name, n in sorted(sector_grades.items(), key=lambda kv: -kv[1])
                ]
            )
            st.dataframe(sector_df, hide_index=True, use_container_width=True)
    else:
        st.write(
            "Weights are at uniform defaults. Grading starts once the first suggestions "
            "mature (~1 week of daily runs)."
        )
    st.divider()
    st.subheader("🧳 Your brain travels with you")
    st.caption(
        "Everything StockSage has learned lives in one file. Take it to another "
        "device, or merge two devices' knowledge together — merging only ever "
        "adds. Robinhood credentials are never part of the brain."
    )
    b1, b2 = st.columns(2)
    with b1:
        from datetime import date as _bdate

        from stocksage.brain import export_brain

        export_path = Path(tempfile.gettempdir()) / "stocksage-brain-export.db"
        export_brain(export_path)
        st.download_button(
            "⬇️ Export brain",
            data=export_path.read_bytes(),
            file_name=f"stocksage-brain-{_bdate.today().isoformat()}.db",
            mime="application/x-sqlite3",
            use_container_width=True,
        )
    with b2:
        uploaded = st.file_uploader("Import a brain file", type=["db"])
        if uploaded is not None and st.button("🧠 Merge into this device", type="primary"):
            from stocksage.brain import import_brain

            tmp = Path(tempfile.gettempdir()) / "stocksage-brain-import.db"
            tmp.write_bytes(uploaded.getvalue())
            try:
                stats = import_brain(tmp)
            except sqlite3.DatabaseError:
                st.error(
                    "That file isn't a StockSage brain (or it's damaged) — "
                    "nothing was changed. Export a fresh one from your other "
                    "device and try again."
                )
            else:
                st.success(
                    f"Merged: +{stats['suggestions_added']} suggestions, "
                    f"+{stats['move_events_added']} move events, weights kept from "
                    f"the {stats['weights_taken_from']} brain."
                )

    recent = db.recent_suggestions(30)
    if recent:
        st.subheader("Recent recorded suggestions")
        rows = [
            {
                "When": r["created_at"][:10],
                "Ticker": r["ticker"],
                "Action": r["action"],
                "Score": round(r["score"], 2),
                "Price": r["price"],
                "Graded": bool(r["evaluated"]),
                "Realized %": round(r["realized_return"] * 100, 2)
                if r["realized_return"] is not None
                else None,
                "Hit": None if r["hit"] is None else bool(r["hit"]),
            }
            for r in recent
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
