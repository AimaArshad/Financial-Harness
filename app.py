"""
Financial Harness dashboard.  Run:  streamlit run app.py
Portfolio + goals + candlestick chart + paper-tested suggestion cards.
"""
import hmac
from datetime import datetime


import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import dashboard_data as dd
import json

st.set_page_config(page_title="Financial Harness", page_icon="📈", layout="wide")

# ---------------- optional password gate (set APP_PASSWORD in Secrets when deployed) ----------------
_password = dd.get_secret("APP_PASSWORD")
if _password and not st.session_state.get("authed"):
    st.title("📈 Financial Harness")
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), str(_password).encode()):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

_default_fake = str(dd.get_secret("FH_DEFAULT_FAKE", "")).lower() in ("1", "true", "yes")

ACTION_BADGE = {
    "BUY": ":green-background[**BUY**]", "ACCUMULATE": ":blue-background[**ACCUMULATE**]",
    "HOLD": ":gray-background[**HOLD**]", "SELL": ":red-background[**SELL**]",
}
money = lambda x: f"${x:,.2f}"


def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        return iso


# ---------------- cached data ----------------
@st.cache_data(ttl=60, show_spinner="Fetching prices via MCP...")
def cached_portfolio(fake: bool):
    return dd.get_portfolio_view(fake)


@st.cache_data(ttl=300, show_spinner="Fetching candles via MCP...")
def cached_candles(symbol: str, period: str, fake: bool):
    return dd.get_candles(symbol, period, fake)


# ---------------- sidebar ----------------
with st.sidebar:
    st.header("📈 Financial Harness")
    engine = st.selectbox("Suggestion engine", list(dd.ENGINES), format_func=dd.ENGINES.get,
                          help="The sample engine needs no API key. The AI engines need ANTHROPIC_API_KEY.")
    fake = st.checkbox("Use synthetic data (offline testing)", value=_default_fake,
                       help="Fake prices. Use when Yahoo is slow or you are offline.")
    if st.button("🔄 Refresh market data"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption("All suggestions are paper-tested with virtual money. No real trades are ever placed.")

st.title("Financial Harness: Portfolio Manager")
st.warning("**Demo only. Not financial advice.** Suggestions are tested with virtual money before they are shown.")
if fake:
    st.info("Showing **synthetic** data for testing, not real market prices.")

# ---------------- portfolio ----------------
try:
    pv = cached_portfolio(fake)
except Exception as e:
    st.error(f"Could not load market data: {e}")
    st.info("Try again in a minute, or tick **Use synthetic data** in the sidebar.")
    st.stop()

st.subheader("1. Your portfolio")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total value", money(pv["total_value"]))
c2.metric("Holdings P&L", money(pv["pnl"]), f"{pv['pnl_pct']:+.2f}%")
c3.metric("Holdings value", money(pv["holdings_value"]))
c4.metric("Cash", money(pv["cash"]))

df = pd.DataFrame(pv["rows"])[["symbol", "shares", "avg_cost", "price", "value", "pnl", "pnl_pct", "weight_pct"]]
df.columns = ["Symbol", "Shares", "Avg cost", "Price", "Value", "P&L ($)", "P&L (%)", "Weight (%)"]
colour = lambda v: "color: #d62728" if v < 0 else "color: #2ca02c"
st.dataframe(
    df.style.map(colour, subset=["P&L ($)", "P&L (%)"]).format(
        {"Avg cost": "{:,.2f}", "Price": "{:,.2f}", "Value": "{:,.2f}", "P&L ($)": "{:+,.2f}",
         "P&L (%)": "{:+.2f}", "Weight (%)": "{:.1f}"}),
    hide_index=True, width="stretch")
st.caption(f"Prices as of {fmt_time(pv['as_of'])} · source: {pv['source']}")

# ---------------- goals ----------------
st.subheader("2. Your goal")
goal_names = [g["name"] for g in pv["goals"]]
goal_name = st.radio("Goal horizon", goal_names, horizontal=True, label_visibility="collapsed")
goal = next(g for g in pv["goals"] if g["name"] == goal_name)
target = goal.get("target_value")
if target:
    progress = pv["total_value"] / target
    g1, g2 = st.columns([2, 1])
    g1.progress(min(max(progress, 0.0), 1.0), text=f"{money(pv['total_value'])} of {money(target)} target ({progress * 100:.0f}%)")
    if pv["total_value"] >= target:
        g2.success("Target reached")
    else:
        years = goal["months"] / 12
        need = ((target / pv["total_value"]) ** (1 / years) - 1) * 100
        g2.metric(f"Return needed per year ({goal['months']} months)", f"{need:.1f}%",
                  help="Simple arithmetic from your current value to the target. It is not a forecast.")

# ---------------- chart ----------------
st.subheader("3. Market trends")
k1, k2 = st.columns([1, 1])
symbol = k1.selectbox("Stock", [r["symbol"] for r in pv["rows"]])
period = k2.selectbox("Period", ["1mo", "3mo", "6mo", "1y"], index=2)
try:
    cd = cached_candles(symbol, period, fake)
    cdf = pd.DataFrame(cd["candles"])
    cdf["time"] = pd.to_datetime(cdf["time"], utc=True)
    cdf["sma20"] = cdf["close"].rolling(20).mean()
    cdf["sma50"] = cdf["close"].rolling(50).mean()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=cdf["time"], open=cdf["open"], high=cdf["high"], low=cdf["low"],
                                 close=cdf["close"], name=symbol), row=1, col=1)
    fig.add_trace(go.Scatter(x=cdf["time"], y=cdf["sma20"], name="20-day avg", line=dict(width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=cdf["time"], y=cdf["sma50"], name="50-day avg", line=dict(width=1.5)), row=1, col=1)
    fig.add_trace(go.Bar(x=cdf["time"], y=cdf["volume"], name="Volume", marker_color="#8899aa"), row=2, col=1)
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(orientation="h", y=1.04))
    st.plotly_chart(fig, width="stretch")
    st.caption(f"{symbol}: {cd['count']} daily candles · data as of {fmt_time(cd['as_of'])} · source: {cd['source']}")
except Exception as e:
    st.error(f"Could not load candles for {symbol}: {e}")

# ---------------- suggestions ----------------
st.subheader(f"4. Suggestions for your {goal_name} goal")
results = st.session_state.setdefault("results", {})
key = (goal_name, engine, fake)
if st.button(f"✨ Generate suggestions ({dd.ENGINES[engine]})", type="primary"):
    with st.spinner("Analysing your portfolio and paper-testing every suggestion with virtual money... "
                    "(AI engines can take up to a minute)"):
        results[key] = dd.get_suggestions(goal_name, engine, fake)


def render_card(item: dict, blocked: bool = False):
    t = item["paper_test"]
    with st.container(border=True):
        a, b = st.columns([1, 1])
        a.markdown(f"#### {item['symbol']}")
        b.markdown(ACTION_BADGE.get(item["action"], item["action"]))
        st.caption(f"{item['horizon'].replace('_', '-')} · confidence: {item['confidence']}")
        st.write(item["reasoning"])
        if item.get("stop_loss"):
            st.caption(f"Stop-loss: {money(item['stop_loss'])}")
        if blocked:
            st.error("**Blocked by guardrails:** " + " ".join(t["reasons"]))
        else:
            o = t.get("order")
            if o:
                st.success(f"Paper-tested ✓ virtual {o['side']} of {o['shares']} shares at {money(o['fill_price'])}")
            else:
                st.success("Paper-tested ✓ no trade needed")
            m = t.get("metrics") or {}
            if m:
                m1, m2, m3 = st.columns(3)
                if m.get("risk_at_stop_pct") is not None:
                    m1.metric("Risk at stop", f"{m['risk_at_stop_pct']:.2f}%")
                m2.metric("Position weight", f"{m['position_weight_after_pct']:.1f}%")
                if m.get("stress_loss_pct") is not None:
                    m3.metric("Stress loss", f"{m['stress_loss_pct']:.2f}%",
                              help=f"Loss if the worst 5-day drop in 6 months ({m['worst_5d_return_pct']}%) repeated.")
        st.caption(f"Data as of {fmt_time(item['data_as_of'])} · Demo only")


def render_group(title: str, items: list):
    st.markdown(f"##### {title}")
    if not items:
        st.caption("None passed the paper test.")
        return
    cols = st.columns(2)
    for i, item in enumerate(items):
        with cols[i % 2]:
            render_card(item)


res = results.get(key)
if res is None:
    st.info("Click **Generate suggestions** to analyse this goal.")
elif "error" in res:
    st.error(res["error"])
else:
    st.markdown(f"**Summary:** {res['summary']}")
    cnt = res["counts"]
    st.caption(f"Engine: {res.get('engine', '')} · {cnt['approved']} approved, {cnt['blocked']} blocked of "
               f"{cnt['submitted']} submitted · generated {fmt_time(res['generated_at'])}")
    render_group("Long-term (Hold / Accumulate)", [s for s in res["suggestions"] if s["horizon"] == "long_term"])
    render_group("Short-term (Buy / Sell)", [s for s in res["suggestions"] if s["horizon"] == "short_term"])
    if res["blocked"]:
        with st.expander(f"🚫 Blocked by guardrails ({len(res['blocked'])})"):
            bc = st.columns(2)
            for i, item in enumerate(res["blocked"]):
                with bc[i % 2]:
                    render_card(item, blocked=True)
    st.caption(res["disclaimer"])

    with st.expander("💼 Virtual (paper) account: before and after"):
        pa = res["paper_account"]
        v1, v2, v3 = st.columns(3)
        v1.metric("Virtual value", money(pa["after"]["total_value"]), money(pa["after"]["total_value"] - pa["before"]["total_value"]))
        v2.metric("Virtual cash", money(pa["after"]["cash"]), money(pa["after"]["cash"] - pa["before"]["cash"]))
        v3.metric("Realized P&L", money(pa["after"]["realized_pnl"]))
        st.dataframe(pd.DataFrame(pa["after"]["positions"]), hide_index=True, width="stretch")
        if pa["trades"]:
            st.markdown("**Virtual trades**")
            st.dataframe(pd.DataFrame(pa["trades"]), hide_index=True, width="stretch")

# with st.expander("🧾 Audit log (last 15 events)"):
#     events = dd.read_audit_log()
#     if events:
#         st.dataframe(pd.DataFrame(events), hide_index=True, width="stretch")
#     else:
#         st.caption("No events yet. The AI engines write here.")


with st.expander("🧾 Audit log (last 15 events)"):
    events = dd.read_audit_log()
    if events:
        log_df = pd.DataFrame(events)
        for col in log_df.columns:
            log_df[col] = log_df[col].apply(
                lambda v: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v)
        st.dataframe(log_df, hide_index=True, width="stretch")
    else:
        st.caption("No events yet. The AI engines write here.")