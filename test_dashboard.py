"""
Tests the Streamlit page headlessly (no browser, no internet, no API key).
Run:  python test_dashboard.py
"""
from streamlit.testing.v1 import AppTest


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        raise SystemExit(1)


def texts(at):
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption) \
        + " " + " ".join(str(s.value) for s in at.success) + " " + " ".join(str(e.value) for e in at.error)


at = AppTest.from_file("app.py", default_timeout=120)
at.run()
check("page loads", not at.exception, str(at.exception))
# turn on synthetic data (checkbox) and reload
at.sidebar.checkbox[0].check().run()
check("page loads with synthetic data", not at.exception, str(at.exception))
check("4 portfolio metrics shown", len(at.metric) >= 4)
labels = [m.label for m in at.metric]
check("total value + P&L + cash metrics", {"Total value", "Holdings P&L", "Cash"} <= set(labels), str(labels))
check("goal selector offers 3 goals", at.radio[0].options == ["10 Months", "20 Months", "10 Years"], str(at.radio[0].options))
check("stock selector lists the holdings", at.selectbox[0].options == ["AAPL", "TSLA", "NVDA"], str(at.selectbox[0].options))
check("candlestick chart rendered", len(at.get("plotly_chart")) == 1)
check("disclaimer is visible", any("Not financial advice" in str(w.value) for w in at.warning))

# generate suggestions with the rule-based engine (also runs the paper trader)
for goal in ["10 Months", "10 Years"]:
    at.radio[0].set_value(goal).run()
    check(f"goal switches to {goal}", not at.exception, str(at.exception))
    gen = [b for b in at.button if "Generate suggestions" in b.label][0]
    gen.click().run()
    real_errors = [e.value for e in at.error if "Blocked by guardrails" not in str(e.value)]  # blocked cards are expected
    check(f"[{goal}] suggestions generated without errors", not at.exception and not real_errors, str(real_errors))
    t = texts(at)
    check(f"[{goal}] cards show paper-tested badge", "Paper-tested" in t)
    check(f"[{goal}] long-term and short-term groups shown", "Long-term (Hold / Accumulate)" in t and "Short-term (Buy / Sell)" in t)
    check(f"[{goal}] data timestamp shown on cards", "Data as of" in t)
    check(f"[{goal}] engine and counts line shown", "approved" in t and "blocked" in t)

# changing the stock updates the chart without errors
at.selectbox[0].set_value("NVDA").run()
check("switching stock works", not at.exception, str(at.exception))
print("\nALL DASHBOARD TESTS PASSED")
