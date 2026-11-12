"""Shared helpers: portfolio loading, MCP client, tool whitelist, audit log."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport


ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = ROOT / "harness" / "mcp_server" / "server.py"
LOG_DIR = Path(os.environ.get("FH_LOG_DIR", ROOT / "logs"))
LOG_PATH = LOG_DIR / "agent_log.jsonl"
SANDBOX_ALLOWED_TOOLS = {"get_portfolio", "get_price", "get_candles", "get_fundamentals", "get_indicators"}

def load_portfolio() -> dict:
    return json.loads((ROOT/"data" / "portfolio.json").read_text(encoding="utf-8"))


def make_mcp_client(fake=None) -> Client:
    """Client that launches server.py as a subprocess (stdio).
    fake=True serves synthetic data; fake=None falls back to the FH_FAKE_DATA=1 environment variable."""
    if fake is None:
        fake = os.environ.get("FH_FAKE_DATA") == "1"
    env = {"FH_FAKE_DATA": "1"} if fake else None
    return Client(PythonStdioTransport(script_path=SERVER_PATH, env=env))


def unwrap(result):
    """Turn an MCP call result into plain Python data."""
    if getattr(result, "data", None) is not None:
        return result.data
    if getattr(result, "content", None):
        text = result.content[0].text
        try:
            return json.loads(text)
        except Exception:
            return text
    return None


async def call_mcp(mcp_client, name: str, args: dict):
    """Execute one tool: local get_portfolio, or an MCP tool."""
    if name == "get_portfolio":
        return load_portfolio()
    return unwrap(await mcp_client.call_tool(name, args))


def make_sandbox_tool_handler(mcp_client):
    """Handler the sandbox uses to call tools. Enforces the whitelist."""
    async def handler(name: str, args: dict):
        if name not in SANDBOX_ALLOWED_TOOLS:
            raise PermissionError(f"Tool '{name}' is not allowed in the sandbox. "
                                  f"Allowed: {sorted(SANDBOX_ALLOWED_TOOLS)}")
        return await call_mcp(mcp_client, name, args)
    return handler


def log_event(kind: str, **data):
    """Append one line to agent_log.jsonl (simple audit log)."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **data}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str)[:5000] + "\n")


class McpData:
    """Market data for the paper trader. The HARNESS fetches its own data (never trusts numbers from the model)."""
    def __init__(self, mcp_client):
        self.client = mcp_client

    async def get_price(self, symbol: str) -> dict:
        return await call_mcp(self.client, "get_price", {"symbol": symbol})

    async def get_candles(self, symbol: str, period: str = "6mo", interval: str = "1d") -> dict:
        return await call_mcp(self.client, "get_candles", {"symbol": symbol, "period": period, "interval": interval})
