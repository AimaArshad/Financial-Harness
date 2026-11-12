"""
Simple code sandbox for the Financial Harness (MVP).

run_code(code, tool_handler) runs agent-written Python in a separate process with:
  - a hard timeout (process is killed)
  - restricted builtins + import allow-list (no network, no files, no os/subprocess)
  - a minimal environment (API keys are NOT passed to the child)
  - tools only via the `tools` bridge, filtered by the harness whitelist
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).parent / "sandbox_runner.py"
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not any(h in k.upper() for h in SECRET_HINTS)}


async def run_code(code: str, tool_handler, timeout: float = 60.0) -> dict:
    """Returns {"status": "ok"|"error"|"timeout", "stdout":..., "result":..., "error":...}."""
    with tempfile.TemporaryDirectory() as tmp:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(RUNNER),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=tmp, env=_clean_env(),
            limit=32 * 1024 * 1024,  # candle data lines can be large
        )

        async def converse():
            proc.stdin.write(json.dumps({"code": code}).encode() + b"\n")
            await proc.stdin.drain()
            while True:
                line = await proc.stdout.readline()
                if not line:
                    err = (await proc.stderr.read()).decode(errors="replace")[-1000:]
                    return {"status": "error", "stdout": "",
                            "error": "Sandbox process exited unexpectedly. " + err}
                msg = json.loads(line)
                kind = msg.get("type")
                if kind == "tool_call":
                    try:
                        reply = {"ok": True, "result": await tool_handler(msg["name"], msg["args"])}
                    except Exception as e:
                        reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    proc.stdin.write(json.dumps(reply, default=str).encode() + b"\n")
                    await proc.stdin.drain()
                elif kind == "result":
                    return {"status": "ok", "stdout": msg["stdout"], "result": msg["result"]}
                elif kind == "error":
                    return {"status": "error", "stdout": msg.get("stdout", ""), "error": msg["error"]}

        try:
            return await asyncio.wait_for(converse(), timeout)
        except asyncio.TimeoutError:
            return {"status": "timeout", "stdout": "",
                    "error": f"Code ran longer than {timeout}s and was killed."}
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
