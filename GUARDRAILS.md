# Financial Harness: 3 Core Guardrail Rules (MVP, as implemented)

All limits live at the top of `guardrails.py`. Change them there.

## Rule 1: Max risk per trade
- The **harness sizes every order**, never the model.
- BUY: the loss if the stop-loss is hit is at most **2% of portfolio value**.
  Every BUY needs a stop-loss below the price, and not closer than 1% to it.
- No single stock may exceed **30% of the portfolio** after a buy.
- ACCUMULATE (long-term, no stop-loss): buys at most **5% of portfolio value**.
- SELL reduces risk, so it needs no stop-loss. It sells up to 50% of the shares held. No short selling.
- Stress test on every BUY / ACCUMULATE: a repeat of the stock's worst 5-day drop in the last 6 months
  must not cost more than **5% of the portfolio**.

## Rule 2: No real-money execution
- Every order passes through one function, `PaperAccount.execute()`, which calls `assert_paper_only()`.
- Any order mode other than `"paper"` raises `RealTradingBlocked`. There is no setting that turns real trading on.
- The agent has no order or trade tool. Code in the sandbox can only call `get_portfolio`, `get_price`, `get_candles`.
- No broker library exists in the project (checked by a test).
- Future: real execution would need explicit human approval for each order, logged.

## Rule 3: Fresh, verified, and labelled data
- Every suggestion carries `data_as_of` copied from the tool output.
- Data older than **15 minutes** (or missing) is rejected, both when the model submits and again when the harness paper-tests.
- The harness fetches its own prices and candles for the paper test. It never trusts numbers from the model.
- A suggestion is shown only after it passes the paper test. Blocked ones are listed separately with reasons.
- Every result carries: "Demo only. Not financial advice."

## Where each rule is enforced
| Rule | File | Function |
|---|---|---|
| 1 | `guardrails.py` | `plan_order()`, `check_after_trade()` |
| 1 | `paper_trader.py` | stress test in `test_suggestion()` |
| 2 | `guardrails.py` | `assert_paper_only()` |
| 2 | `paper_trader.py` | `PaperAccount.execute()` |
| 3 | `schemas.py` | `validate_suggestions()` |
| 3 | `paper_trader.py` | data-age check in `test_suggestion()` |
