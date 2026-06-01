from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .orchestrator import commands_from_config
from .state import HarnessState
from .verifier import Verifier


_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


TOOLS = [
    {
        "name": "rsi_create_task",
        "description": "Create a persistent RSI task.",
        "inputSchema": {
            "type": "object",
            "properties": {"spec": {"type": "string"}},
            "required": ["spec"],
        },
    },
    {
        "name": "rsi_run_verify",
        "description": "Run configured verification commands and return executable evidence.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rsi_select_winner",
        "description": "Return the winner recorded for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    },
]


def main() -> int:
    for line in sys.stdin:
        response = process_line(line)
        if response is not None:
            print(response, flush=True)
    return 0


def process_line(line: str) -> str | None:
    """Turn one stdin line into a JSON-RPC response string (or None for blank lines).

    A malformed line yields a -32700 parse error rather than crashing the loop.
    """
    if not line.strip():
        return None
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}})
    return json.dumps(handle_request(request))


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
    if name == "rsi_create_task":
        task = HarnessState().create_task(str(args["spec"]), {"source": "mcp"})
        return _text(task.path)
    if name == "rsi_run_verify":
        config = load_config()
        report = Verifier(timeout_sec=config.verify.timeout_sec).run(commands_from_config(config), Path.cwd())
        return _text(report.to_json())
    if name == "rsi_select_winner":
        raw_task_id = args.get("task_id")
        task_id = _validate_task_id(str(raw_task_id)) if raw_task_id else HarnessState().latest_task_id()
        selection = HarnessState().task_dir(task_id) / "selection.json"
        return _text(selection.read_text(encoding="utf-8"))
    raise ValueError(f"Unknown tool: {name}")


def _validate_task_id(task_id: str) -> str:
    if ".." in task_id or "/" in task_id or "\\" in task_id or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    return task_id


def _text(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}]}


if __name__ == "__main__":
    raise SystemExit(main())
