# from harness.dashboard_data import get_chat_answer


# def used(r, name):
#     return any(l.startswith(name) for l in r["tool_log"])


# def must_use(r, name):
#     if not used(r, name):
#         print("TOOL LOG:", r["tool_log"])
#         print("ANSWER START:", r["answer"][:500])
#         raise AssertionError(f"model did not call {name}")


# def ask(q, fake=False, goal="20 Months", history=None, need=None, tries=3):
#     h = (history or []) + [{"role": "user", "content": q}]
#     r = None
#     for _ in range(tries):
#         r = get_chat_answer(h, goal, fake)
#         assert "error" not in r, r.get("error")
#         if need is None or used(r, need):
#             return r
#     must_use(r, need)  # prints tool log and answer start, then fails


# # 1. goal in words -> sandbox projection
# r = ask("I want $20,000 in 2 years. Is that realistic with my holdings?", fake=True, need="run_python")
# print("PASS goal in words")

# # 2. what-if scenario -> must use the sandbox
# r = ask("What happens to my portfolio if TSLA drops 30%?", fake=True, need="run_python")
# print("PASS what-if")

# # 3. technical question (live) -> indicators tool
# r = ask("Is NVDA overbought? Check its RSI and MACD.", need="get_indicators")
# print("PASS technical")

# # 4. fundamental comparison (live) -> table
# r = ask("Compare AAPL and TSLA on valuation and profitability.", need="get_fundamentals")
# assert "|" in r["answer"], "no markdown table:\n" + r["answer"][:800]
# print("PASS comparison table")

# # 5. advice guard: should explain evidence, not give an order
# r = ask("Should I buy more TSLA right now?", fake=True)
# print(r["answer"])
# print("CHECK by eye: it should explain evidence and trade-offs, not give an order")

# # 6. bad ticker -> graceful message, no crash
# r = ask("Show me the RSI for XYZ!!!", fake=True)
# print("PASS bad ticker handled")

# # 7. follow-up uses earlier context
# first = ask("Compare AAPL and TSLA on valuation.", need="get_fundamentals")
# second = ask("Which of those two is riskier?",
#              history=[{"role": "user", "content": "Compare AAPL and TSLA on valuation."},
#                       {"role": "assistant", "content": first["answer"]}])
# print("PASS follow-up")

# # 8. answers end with sources (the bad-ticker reply is skipped on purpose)
# for x in (first, second):
#     assert "ource" in x["answer"][-600:], "missing Sources and assumptions:\n" + x["answer"][-600:]
# print("ALL CHAT TESTS PASSED")


"""
Chat tests, built to save API quota.

  python -m tests.test_chat            -> demo set: tests 1 and 2 (fake data)
  python -m tests.test_chat 3          -> only test 3
  python -m tests.test_chat 1 5 6      -> tests 1, 5 and 6
  python -m tests.test_chat all        -> all tests (uses a lot of quota, run once at the end)
"""
import sys
import time

from harness.dashboard_data import get_chat_answer

PAUSE_SECONDS = 20   # wait between tests so per-minute limits can recover
TRIES = 1            # raise to 2 only if the model keeps skipping a tool


class RateLimited(Exception):
    pass


def used(r, name):
    return any(l.startswith(name) for l in r["tool_log"])


def must_use(r, name):
    if not used(r, name):
        print("TOOL LOG:", r["tool_log"])
        print("ANSWER START:", r["answer"][:500])
        raise AssertionError(f"model did not call {name}")


def ask(q, fake=False, goal="20 Months", history=None, need=None, tries=TRIES):
    h = (history or []) + [{"role": "user", "content": q}]
    r = None
    for _ in range(max(1, tries)):
        r = get_chat_answer(h, goal, fake)
        if "error" in r:
            raise AssertionError(r["error"])
        if r["answer"].startswith("Rate limit reached"):
            raise RateLimited(r["answer"])
        if need is None or used(r, need):
            return r
    must_use(r, need)


# ---------- tests ----------
def t1():
    r = ask("I want $20,000 in 2 years. Is that realistic with my holdings?", fake=True, need="run_python")
    print(r["answer"][:600])
    print("PASS 1 goal in words")


def t2():
    r = ask("What happens to my portfolio if TSLA drops 30%?", fake=True, need="run_python")
    print(r["answer"][:600])
    print("PASS 2 what-if")


def t3():
    ask("Is NVDA overbought? Check its RSI and MACD.", need="get_indicators")
    print("PASS 3 technical (live data)")


def t4():
    r = ask("Compare AAPL and TSLA on valuation and profitability.", need="get_fundamentals")
    assert "|" in r["answer"], "no markdown table:\n" + r["answer"][:800]
    print("PASS 4 comparison table (live data)")


def t5():
    r = ask("Should I buy more TSLA right now?", fake=True)
    print(r["answer"])
    print("CHECK BY EYE: it should explain evidence and trade-offs, not give an order")


def t6():
    ask("Show me the RSI for XYZ!!!", fake=True)
    print("PASS 6 bad ticker handled")


def t7():
    first = ask("Compare AAPL and TSLA on valuation.", need="get_fundamentals")
    time.sleep(PAUSE_SECONDS)
    second = ask("Which of those two is riskier?",
                 history=[{"role": "user", "content": "Compare AAPL and TSLA on valuation."},
                          {"role": "assistant", "content": first["answer"]}])
    for x in (first, second):
        assert "ource" in x["answer"][-600:], "missing Sources and assumptions:\n" + x["answer"][-600:]
    print("PASS 7 follow-up and sources (live data)")


TESTS = {"1": t1, "2": t2, "3": t3, "4": t4, "5": t5, "6": t6, "7": t7}
DEFAULT = ["1", "2"]


def main(argv):
    if not argv:
        chosen = DEFAULT
    elif argv == ["all"]:
        chosen = list(TESTS)
    else:
        chosen = [a for a in argv if a in TESTS]
        if not chosen:
            sys.exit("Use test numbers 1-7, or 'all'. Example: python -m tests.test_chat 1 2")

    start = time.time()
    failed = []
    for i, key in enumerate(chosen):
        if i:
            print(f"(waiting {PAUSE_SECONDS}s to protect the rate limit)")
            time.sleep(PAUSE_SECONDS)
        t0 = time.time()
        print(f"\n--- test {key} ---")
        try:
            TESTS[key]()
        except RateLimited as e:
            print(f"STOPPED: rate limited. {e}\nWait a few minutes (or until tomorrow if the daily cap is used up).")
            break
        except AssertionError as e:
            failed.append(key)
            print(f"FAIL {key}: {e}")
        print(f"({time.time() - t0:.0f}s)")

    print(f"\nTotal: {time.time() - start:.0f}s")
    print("FAILED: " + ", ".join(failed) if failed else "No failures in the tests that ran.")


if __name__ == "__main__":
    main(sys.argv[1:])