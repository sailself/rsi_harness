from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .cli import cmd_select
from .config import load_config
from .orchestrator import commands_from_config
from .state import HarnessState
from .verifier import Verifier


TOOLS = [
    {
        "name": "rsi.create_task",
        "description": "Create a persistent RSI task.",
        "inputSchema": {
            "type": "object",
            "properties": {"spec": {"type": "string"}},
            "required": ["spec"],
        },
    },
    {
        "name": "rsi.run_verify",
        "description": "Run configured verification commands and return executable evidence.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rsi.select_winner",
        "description": "Return the winner recorded for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    },
]


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        response = handle_request(request)
        print(json.dumps(response), flush=True)
    return 0


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    request_id = request.get("id")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rsi-harness", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _call_tool(request.get("params") or {})
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:  # pragma: no cover - defensive MCP boundary
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "rsi.create_task":
        task = HarnessState().create_task(str(args["spec"]), {"source": "mcp"})
        return _text(task.path)
    if name == "rsi.run_verify":
        config = load_config()
        report = Verifier(timeout_sec=config.verify.timeout_sec).run(commands_from_config(config), Path.cwd())
        return _text(report.to_json())
    if name == "rsi.select_winner":
        task_id = str(args.get("task_id") or HarnessState().latest_task_id())
        selection = HarnessState().task_dir(task_id) / "selection.json"
        return _text(selection.read_text(encoding="utf-8"))
    raise ValueError(f"Unknown tool: {name}")


def _text(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}]}


if __name__ == "__main__":
    raise SystemExit(main())
