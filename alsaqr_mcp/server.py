"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), no third-party dependencies.

Implements: initialize, notifications/initialized, ping, tools/list, tools/call.
"""
import json
import sys
import traceback

from . import tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "alsaqr-mcp", "version": "0.1.0"}


def _reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(msg: dict):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        _reply(id_, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    elif method == "notifications/initialized" or method is None:
        return
    elif method == "ping":
        _reply(id_, {})
    elif method == "tools/list":
        _reply(id_, {"tools": tools.list_tools()})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = tools.call(name, args)
            is_err = isinstance(result, dict) and "error" in result
            _reply(id_, {"content": [{"type": "text", "text": json.dumps(result, indent=1)}], "isError": is_err})
        except tools.UnknownTool:
            _reply(id_, error={"code": -32601, "message": f"unknown tool {name}"})
        except Exception as e:  # tool crash: report, keep serving
            _reply(id_, {"content": [{"type": "text", "text": json.dumps(
                {"error": "tool crashed", "fix": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})}],
                "isError": True})
    else:
        if id_ is not None:
            _reply(id_, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
