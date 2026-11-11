"""
Runs INSIDE the sandbox subprocess. Do not run this file yourself; sandbox.py starts it.

What it does:
  - receives agent-written code on stdin
  - runs it with restricted builtins and an import allow-list
  - gives the code a `tools` object; each tools.xxx(...) call is sent to the harness
    (which decides if it's allowed) and the answer comes back
  - captures print() output and the final `result` variable
Protocol: one JSON message per line.
NOTE: this is a basic MVP sandbox. For real isolation use Docker / E2B / a cloud sandbox.
"""
import builtins
import io
import json
import os
import sys
import traceback

ALLOWED_IMPORTS = {
    "math", "statistics", "json", "datetime", "collections", "itertools", "functools",
    "re", "decimal", "fractions", "random", "heapq", "bisect", "typing", "dataclasses",
}
BLOCKED_BUILTINS = {"open", "input", "breakpoint", "exit", "quit", "help",
                    "exec", "eval", "compile", "globals", "locals", "vars"}
MAX_OUTPUT_CHARS = 20000

# Unix only: cap memory and CPU time (silently skipped on Windows)
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
except Exception:
    pass

sys.stdin.reconfigure(encoding="utf-8")
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8")  # private channel to the harness
_real_import = builtins.__import__


def _send(msg: dict):
    _proto.write(json.dumps(msg, default=str) + "\n")
    _proto.flush()


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Only restrict imports made by the agent's code (its globals are named __sandbox__)
    if globals is not None and globals.get("__name__") == "__sandbox__":
        if level != 0 or name.split(".")[0] not in ALLOWED_IMPORTS:
            raise ImportError(f"Import of '{name}' is not allowed in the sandbox. "
                              f"Allowed modules: {sorted(ALLOWED_IMPORTS)}")
    return _real_import(name, globals, locals, fromlist, level)


class _Tools:
    """tools.get_price(symbol="AAPL") -> sent to the harness, result returned."""
    def __init__(self):
        self._n = 0

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kwargs):
            self._n += 1
            _send({"type": "tool_call", "id": self._n, "name": name, "args": kwargs})
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("Harness closed the connection")
            reply = json.loads(line)
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "tool error"))
            return reply["result"]
        return call


def main():
    code = json.loads(sys.stdin.readline())["code"]
    captured = io.StringIO()
    sys.stdout = captured  # user print() goes here, not to the protocol channel

    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in BLOCKED_BUILTINS}
    safe_builtins["__import__"] = _guarded_import
    env = {"__name__": "__sandbox__", "__builtins__": safe_builtins, "tools": _Tools()}
    try:
        exec(compile(code, "<agent_code>", "exec"), env)
        _send({"type": "result", "stdout": captured.getvalue()[:MAX_OUTPUT_CHARS],
               "result": env.get("result")})
    except BaseException:
        _send({"type": "error", "error": traceback.format_exc()[-1500:],
               "stdout": captured.getvalue()[:MAX_OUTPUT_CHARS]})


main()
