"""CLI: `python3 -m alsaqr_mcp serve` (MCP over stdio) or `python3 -m alsaqr_mcp <tool> key=value ...`."""
import json
import sys

from . import server, tools


def _parse(argv):
    args = {}
    for a in argv:
        if "=" not in a:
            raise SystemExit(f"expected key=value, got {a!r}")
        k, v = a.split("=", 1)
        if v[:1] in ("[", "{"):
            v = json.loads(v)
        elif v.isdigit():
            v = int(v)
        elif v in ("true", "false"):
            v = v == "true"
        args[k] = v
    return args


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        names = ", ".join(t["name"] for t in tools.list_tools())
        print(f"usage: python3 -m alsaqr_mcp serve | <tool> key=value ...\ntools: {names}")
        return
    if sys.argv[1] == "serve":
        server.main()
        return
    if sys.argv[1] == "list":
        print(json.dumps(tools.list_tools(), indent=1))
        return
    try:
        print(json.dumps(tools.call(sys.argv[1], _parse(sys.argv[2:])), indent=1))
    except tools.UnknownTool as e:
        raise SystemExit(f"unknown tool {e}")


if __name__ == "__main__":
    main()
