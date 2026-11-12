"""
Context management for the harness (goes in harness/context.py).

The model has a limited context window and Groq's free tier also limits tokens per minute, so the harness
decides what the model gets to see. Four mechanisms work together:

  1. trim_history()      - only the most recent chat turns are sent, capped in size
  2. compact_candles()   - (agent_free.py) raw candles are summarised before they reach the model
  3. compress_messages() - when a run grows past a token budget, OLD tool outputs are shortened
  4. result cache        - (agent_free.py) repeated identical tool calls are not sent twice

Rough token estimate: 1 token is about 4 characters of English text. It is an estimate, not an exact count.
"""

CHARS_PER_TOKEN = 4
SHORTENED_NOTE = " ...[older tool output shortened by harness]"


def estimate_tokens(messages: list) -> int:
    """Approximate token count of an OpenAI-style message list."""
    chars = 0
    for m in messages:
        chars += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return chars // CHARS_PER_TOKEN


def trim_history(history: list, max_messages: int = 10, max_chars_each: int = 4000) -> list:
    """Keep only the latest user/assistant turns, cap each message, and start on a user message."""
    h = [{"role": m["role"], "content": str(m["content"])[:max_chars_each]}
         for m in history if m.get("role") in ("user", "assistant")]
    h = h[-max_messages:]
    while h and h[0]["role"] != "user":
        h.pop(0)
    return h


def compress_messages(messages: list, budget_tokens: int = 5000, keep_recent: int = 4) -> list:
    """Return a copy of `messages` that fits the budget where possible.

    Only the CONTENT of old tool results is shortened. No message is removed, so every assistant tool call
    still has its matching tool result (the API rejects conversations where they do not match).
    The system message and the most recent `keep_recent` messages are never touched.
    The original list is not modified, so the full log stays available.
    """
    if estimate_tokens(messages) <= budget_tokens:
        return messages
    out = [dict(m) for m in messages]
    cutoff = max(1, len(out) - keep_recent)
    for limit in (800, 300, 120):
        for m in out[1:cutoff]:
            if m.get("role") == "tool" and len(str(m.get("content", ""))) > limit:
                m["content"] = str(m["content"])[:limit] + SHORTENED_NOTE
        if estimate_tokens(out) <= budget_tokens:
            break
    return out