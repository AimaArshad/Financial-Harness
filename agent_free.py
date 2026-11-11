"""
Financial Harness - agent loop using a FREE LLM (OpenAI-compatible API).
Providers: groq, gemini, ollama. Reuses the same tools, validation, sandbox and paper trader.

  $env:GROQ_API_KEY="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"        (PowerShell)
  python agent_free.py --provider groq --goal "10 Months" --fake
  python agent_free.py --provider groq --goal "10 Years" --mode ptc
"""

import openai
import argparse
import asyncio
import copy
import json
import os
import sys
from pathlib import Path

from agent import (GET_PORTFOLIO_TOOL, RUN_PYTHON_TOOL, SUBMIT_TOOL, SYSTEM_DIRECT, SYSTEM_PTC,
                    _truncate)
from harness_common import (McpData, call_mcp, load_portfolio, log_event, make_mcp_client,
                            make_sandbox_tool_handler)
from paper_trader import print_review, review_suggestions
from schemas import validate_suggestions

# PROVIDERS = {
#     "groq":   {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
#                "model": "llama-3.3-70b-versatile"},
#     "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
#                "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash"},
#     "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": "qwen2.5:7b"},
#     # "openai/gpt-oss-120b"
# }
PROVIDERS={
    "groq":   {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
               "model": "openai/gpt-oss-120b"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": "qwen2.5:7b"},
    # "openai/gpt-oss-120b"
}

def to_openai_tool(t: dict) -> dict:
    schema = copy.deepcopy(t["input_schema"])
    # simplify nullable types, some free providers reject ["number","null"]
    props = schema.get("properties", {})
    for p in props.values():
        pass
    item_props = props.get("suggestions", {}).get("items", {}).get("properties", {})
    if "stop_loss" in item_props:
        item_props["stop_loss"] = {"type": "number",
                                   "description": "Required for BUY (below current price); omit otherwise"}
    return {"type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""), "parameters": schema}}


def build_tools(mode, mcp_tools):
    if mode == "ptc":
        base = [RUN_PYTHON_TOOL, SUBMIT_TOOL]
    else:
        base = [GET_PORTFOLIO_TOOL]
        for t in mcp_tools:
            base.append({"name": t.name, "description": t.description or "", "input_schema": t.inputSchema})
        base.append(SUBMIT_TOOL)
    return [to_openai_tool(t) for t in base]


def compact_candles(data: dict) -> dict:
    """Context management: give the model a summary instead of every raw candle."""
    # candles = data.get("candles", [])
    # closes = [c["close"] for c in candles]
    
    keys = ("open", "high", "low", "close", "volume")
    candles = [c for c in data.get("candles", []) if all(c.get(k) is not None for k in keys)]
    if not candles:
           return {"error": "No complete candles returned", "symbol": data.get("symbol")}
    closes = [c["close"] for c in candles]

    def sma(n):
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    first, last = closes[0], closes[-1]
    return {
        "symbol": data.get("symbol"), "period": data.get("period"), "interval": data.get("interval"),
        "count": len(candles), "last_close": last,
        "sma20": sma(20), "sma50": sma(50),
        "period_high": max(c["high"] for c in candles), "period_low": min(c["low"] for c in candles),
        "change_over_period_pct": round((last - first) / first * 100, 2),
        "avg_volume": int(sum(c["volume"] for c in candles) / len(candles)),
        "last_5_candles": candles[-5:],
        "as_of": data.get("as_of"), "source": data.get("source"),
        "note": "Candles summarized by the harness to save tokens.",
    }


PTC_HELP = """
- Return shapes inside run_python (all are dicts):
  tools.get_portfolio() -> {"cash":..., "holdings":[{"symbol","shares","avg_cost"}], "goals":[...]}
  tools.get_price(symbol="AAPL") -> {"symbol","price","as_of",...}
  tools.get_candles(symbol="AAPL", period="6mo", interval="1d") -> {"candles":[{"time","open","high","low","close","volume"},...], "as_of":...}
  The candle list is under the key "candles". Holdings use the key "shares", not "quantity".
- NEVER print raw candles or whole tool results. Print only a few short summary lines.
- Keep the code short and call submit_suggestions yourself after you see the result."""


def _trim(text, limit=5000):
    return text if len(text) <= limit else text[:limit] + "\n...[trimmed by harness]"

async def run_agent_free(llm, model, mcp_client, mode, goal, max_turns=20, verbose=True, review=True):
    portfolio = load_portfolio()
    held = {h["symbol"].upper() for h in portfolio["holdings"]}
    tools = build_tools(mode, await mcp_client.list_tools())
    # system = SYSTEM_PTC if mode == "ptc" else SYSTEM_DIRECT
    system = (SYSTEM_PTC if mode == "ptc" else SYSTEM_DIRECT) + (
        "\n- You only have price and candle data. Do NOT mention fundamentals, valuation, earnings, "
        "news or company strategy. Use only numbers that appear in tool outputs."
        "\n- confidence must be exactly one of: low, medium, high.")
    if mode == "ptc":
        system += PTC_HELP
        
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Analyse my portfolio for my goal horizon: {goal}. "
                                    f"Give long-term (hold/accumulate) and short-term (buy/sell) suggestions."},
    ]
    log_event("run_start", mode=mode, goal=goal, model=model)
    say = print if verbose else (lambda *a: None)
    
    cache = {}
    fetched = set()
    for turn in range(1, max_turns + 1):
        # resp = await llm.chat.completions.create(model=model, messages=messages, tools=tools,
        #                                          tool_choice="auto", max_tokens=4000)
        try:
            resp = await llm.chat.completions.create(model=model, messages=messages, tools=tools,
                                                     tool_choice="auto", max_tokens=4000)
        except openai.RateLimitError:
            say(f"[turn {turn}] rate limit hit, waiting 30s...")
            await asyncio.sleep(30)
            continue
        except openai.BadRequestError as e:
            say(f"[turn {turn}] invalid tool call from model, asking it to retry: {str(e)[:200]}")
            log_event("llm_bad_request", turn=turn, error=str(e)[:300])
            messages.append({"role": "user",
                             "content": f"Your last tool call was invalid: {str(e)[:300]} Fix it and try again."})
            continue
        
        except openai.APIStatusError as e:
            if e.status_code == 413:
                say(f"[turn {turn}] request too large, trimming old tool outputs and waiting 60s...")
                for m in messages:
                    if m.get("role") == "tool" and len(m["content"]) > 1500:
                        m["content"] = m["content"][:1500] + "...[trimmed by harness]"
                await asyncio.sleep(60)
                continue
            raise
        
        msg = resp.choices[0].message
        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name,
                                                 "arguments": tc.function.arguments or "{}"}}
                                   for tc in msg.tool_calls]
        messages.append(entry)
        if msg.content and msg.content.strip():
            say(f"[turn {turn}] agent: {msg.content.strip()[:300]}")

        if not msg.tool_calls:
            messages.append({"role": "user", "content": "Please finish by calling submit_suggestions."})
            continue

        final = None
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            say(f"[turn {turn}] tool: {name} {json.dumps(args)[:150]}")
            log_event("tool_call", turn=turn, name=name, input=args)
            is_error = False
            try:
                if name == "submit_suggestions":
                    # ok, out = validate_suggestions(args, held)
                    missing = set()
                    if mode == "direct":
                        missing = {str(s.get("symbol", "")).upper() for s in args.get("suggestions", [])} - fetched
                    if missing:
                        ok, out = False, [f"You never fetched candles for {', '.join(sorted(missing))}. "
                                          f"Call get_candles for them, then submit again."]
                    else:
                        ok, out = validate_suggestions(args, held)
                        
                        
                    if ok:
                        final, content = out, "Accepted."
                    else:
                        is_error = True
                        content = ("REJECTED by harness. Fix these and call submit_suggestions again:\n- "
                                   + "\n- ".join(out))
                        say(f"[turn {turn}] harness rejected submission: {out}")
                elif name == "run_python":
                    from sandbox import run_code
                    out = await run_code(args.get("code", ""), make_sandbox_tool_handler(mcp_client), timeout=90)
                    is_error = out["status"] != "ok"
                    # content = _truncate(json.dumps(out, default=str))
                    content = _trim(json.dumps(out, default=str))
                    say(f"[turn {turn}] sandbox status: {out['status']}")
                # else:
                #     if mode == "ptc":
                #         raise PermissionError(f"Tool '{name}' is not available in ptc mode")
                #     # content = _truncate(json.dumps(await call_mcp(mcp_client, name, args), default=str))
                    
                #     result = await call_mcp(mcp_client, name, args)
                #     if name == "get_candles" and isinstance(result, dict) and result.get("candles"):
                #         result = compact_candles(result)
                #     content = _truncate(json.dumps(result, default=str))
                    
                else:
                    if mode == "ptc":
                        raise PermissionError(f"Tool '{name}' is not available in ptc mode")
                    key = name + json.dumps(args, sort_keys=True)
                    if key in cache:
                        content = ("You already called this with the same inputs. Use the earlier result "
                                   "and continue. Previous result: " + cache[key])
                    else:
                        result = await call_mcp(mcp_client, name, args)
                        if name == "get_candles":
                            fetched.add(str(args.get("symbol", "")).upper())
                        if name == "get_candles" and isinstance(result, dict) and result.get("candles"):
                            result = compact_candles(result)
                        content = _truncate(json.dumps(result, default=str))
                        cache[key] = content    
                        
            except Exception as e:
                is_error = True
                content = f"Tool error: {type(e).__name__}: {e}"
            log_event("tool_result", turn=turn, name=name, is_error=is_error, preview=content[:300])
            say(f"[turn {turn}]   -> {'ERROR: ' if is_error else ''}{content[:150]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        if final:
            if review:
                say(f"[turn {turn}] paper-testing {len(final['suggestions'])} suggestions with virtual money...")
                final = await review_suggestions(final, McpData(mcp_client), portfolio)
                if final["blocked"]:
                    names = ", ".join(f'{b["symbol"]} {b["action"]}' for b in final["blocked"])
                    final["summary"] += f" NOTE: the paper test blocked: {names}. Ignore those in the text above."
                log_event("paper_review", counts=final["counts"],
                          blocked=[(b["symbol"], b["action"], b["paper_test"]["reasons"]) for b in final["blocked"]])
            log_event("run_done", turns=turn)
            return final

    log_event("run_failed", reason="max_turns")
    return {"error": f"No valid submission after {max_turns} turns"}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=list(PROVIDERS), default="groq")
    ap.add_argument("--model", default=None, help="override the provider's default model")
    ap.add_argument("--goal", default="10 Months")
    ap.add_argument("--mode", choices=["direct", "ptc"], default="direct")
    ap.add_argument("--fake", action="store_true")
    args = ap.parse_args()
    if args.fake:
        os.environ["FH_FAKE_DATA"] = "1"

    cfg = PROVIDERS[args.provider]
    key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else "ollama"
    if not key:
        sys.exit(f"Set {cfg['key_env']} first.")

    from openai import AsyncOpenAI
    llm = AsyncOpenAI(base_url=cfg["base_url"], api_key=key)
    model = args.model or cfg["model"]
    async with make_mcp_client() as mcp_client:
        result = await run_agent_free(llm, model, mcp_client, args.mode, args.goal)

    print("\n===== FINAL RESULT =====")
    print(json.dumps(result, indent=2))
    if "counts" in result:
        print_review(result)
    Path("suggestions.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\nSaved to suggestions.json (audit log: agent_log.jsonl)")


if __name__ == "__main__":
    asyncio.run(main())