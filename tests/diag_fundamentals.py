from harness.dashboard_data import get_chat_answer
r = get_chat_answer([{"role": "user", "content": "Compare AAPL and TSLA on valuation and profitability."}], "20 Months", False)
print(r.get("error") or "")
print(*r.get("tool_log", []), sep="\n")
print("\nANSWER:\n", r.get("answer"))