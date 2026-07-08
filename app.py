"""StockSage dashboard — run with:  streamlit run app.py

A single-user cockpit over the engine: today's suggestions, sector trends,
your Robinhood portfolio, the move-context memory, and the learning status.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from stocksage.db import Database
from stocksage.engine import Engine
from stocksage.envfile import load_env, save_env
from stocksage.robinhood import RobinhoodClient

load_env()  # pick up .env automatically; real environment still wins

st.set_page_config(page_title="StockSage", page_icon="📈", layout="wide")

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
    with st.spinner(label):
        result = engine.daily_run()
    st.session_state["scan_result"] = result


st.title("📈 StockSage")
st.caption(
    "Your personal learning market engine. Decision support only — every trade is your call."
)

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

tab_sugg, tab_sectors, tab_portfolio, tab_moves, tab_learning = st.tabs(
    ["💡 Suggestions", "🏭 Sectors", "💼 Portfolio", "📰 Why it moved", "🧠 Learning"]
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
    else:
        st.write(
            "Weights are at uniform defaults. Grading starts once the first suggestions "
            "mature (~1 week of daily runs)."
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
