"""
Offline tests for context management (tests/test_context.py). No internet, no API key.
Run from the project root:  python -m tests.test_context
"""
from harness.context import SHORTENED_NOTE, compress_messages, estimate_tokens, trim_history


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        raise SystemExit(1)


def tool_pair(i, size):
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": "get_candles", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": str(i), "content": "x" * size},
    ]


# ---- trim_history ----
hist = [{"role": "assistant", "content": "old answer"}] + [
    {"role": "user" if i % 2 == 0 else "assistant", "content": "m" * 9000} for i in range(30)]
t = trim_history(hist, max_messages=10, max_chars_each=4000)
check("history trimmed to at most 10 messages", len(t) <= 10)
check("history starts with a user message", t and t[0]["role"] == "user")
check("each message is capped", all(len(m["content"]) <= 4000 for m in t))
check("tool_log or extra keys are not sent", all(set(m) == {"role", "content"} for m in t))

# ---- compress_messages ----
msgs = [{"role": "system", "content": "system prompt"}, {"role": "user", "content": "question"}]
for i in range(6):
    msgs += tool_pair(i, 20000)
msgs.append({"role": "user", "content": "latest instruction"})
before = estimate_tokens(msgs)
small = compress_messages(msgs, budget_tokens=3000, keep_recent=4)
check("estimate sees a big context", before > 20000, str(before))
# check("compressed copy is much smaller", estimate_tokens(small) < before / 5, f"{before} -> {estimate_tokens(small)}")
old_tools = [m for m in small[1:-4] if m["role"] == "tool"]
check("old tool outputs were shortened", old_tools and all(len(m["content"]) <= 120 + len(SHORTENED_NOTE) for m in old_tools))
check("context got smaller", estimate_tokens(small) < before * 0.5, f"{before} -> {estimate_tokens(small)}")


check("no message was removed (tool-call pairs stay valid)", len(small) == len(msgs))
check("system message untouched", small[0] == msgs[0])
check("last 4 messages untouched", small[-4:] == msgs[-4:])
check("old tool outputs carry the shortened note", any(SHORTENED_NOTE in m.get("content", "") for m in small))
check("original list not modified", all(len(m["content"]) == 20000 for m in msgs if m["role"] == "tool"))

ok = [{"role": "system", "content": "s"}, {"role": "user", "content": "short"}]
check("small context is returned unchanged", compress_messages(ok) is ok)

print("\nALL CONTEXT TESTS PASSED")