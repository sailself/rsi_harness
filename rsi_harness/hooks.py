from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .orchestrator import changed_commands_from_config
from .state import HarnessState
from .verifier import Verifier


DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-rf\s+[/~]",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fdx\b",
    r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",
]


def run_hook(event: str, stdin: str | None = None, cwd: Path | None = None) -> int:
    payload = _parse_stdin(stdin)
    cwd = cwd or Path.cwd()
    if event == "session-start":
        _emit_context(
            "RSI harness is available. For complex coding tasks, create a measurable task spec, run `rsi verify --json`, preserve executable feedback, and finish with exact verification evidence.",
            "session-start",
        )
        return 0
    if event == "prompt-submit":
        return _prompt_submit(payload, cwd)
    if event == "pre-tool":
        return _pre_tool(payload)
    if event == "post-tool":
        return _post_tool(cwd)
    if event == "stop":
        return _stop(cwd)
    print(json.dumps({"systemMessage": f"Unknown RSI hook event: {event}"}))
    return 0


def _prompt_submit(payload: dict[str, Any], cwd: Path) -> int:
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
    if not _looks_like_complex_coding_task(prompt):
        return 0
    state = HarnessState(cwd / ".rsi")
    task = state.create_task(prompt, {"source": "UserPromptSubmit"})
    _emit_context(f"RSI task created at {task.path}. Keep candidate evidence under this task.", "prompt-submit")
    return 0


def _pre_tool(payload: dict[str, Any]) -> int:
    command = _extract_tool_command(payload)
    if command and any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in DESTRUCTIVE_PATTERNS):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "RSI harness blocked a destructive command. Ask the user before retrying.",
                    }
                }
            )
        )
    return 0


def _post_tool(cwd: Path) -> int:
    config_path = cwd / ".rsi.yaml"
    if not config_path.exists():
        return 0
    config = load_config(config_path)
    commands = changed_commands_from_config(config, cwd)
    if not commands:
        return 0
    report = Verifier(timeout_sec=config.verify.timeout_sec).run(commands, cwd)
    feedback_dir = cwd / ".rsi" / "hook-feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    (feedback_dir / "latest.json").write_text(report.to_json(), encoding="utf-8")
    if not report.hard_pass:
        failed = [result.name for result in report.results if result.exit_code != 0 or result.timed_out]
        _emit_context(
            f"RSI verification failed after the last tool use: {', '.join(failed)}. Read .rsi/hook-feedback/latest.json before continuing.",
            "post-tool",
        )
    return 0


def _stop(cwd: Path) -> int:
    latest = cwd / ".rsi" / "hook-feedback" / "latest.json"
    if not latest.exists():
        return 0
    report = json.loads(latest.read_text(encoding="utf-8"))
    if not report.get("hard_pass", False):
        _emit_context(
            "RSI stop gate: latest verification report is not green. Continue repair or report the residual failing command explicitly.",
            "stop",
        )
    return 0


_HOOK_EVENT_NAMES = {
    "session-start": "SessionStart",
    "prompt-submit": "UserPromptSubmit",
    "post-tool": "PostToolUse",
    "stop": "Stop",
}


def _emit_context(text: str, event: str) -> None:
    # Stop hooks have no additionalContext channel in the host contract, so a
    # top-level systemMessage is the portable way to surface advisory text.
    if event == "stop":
        print(json.dumps({"systemMessage": text}))
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": _HOOK_EVENT_NAMES.get(event, "SessionStart"),
                    "additionalContext": text,
                }
            }
        )
    )


def _looks_like_complex_coding_task(prompt: str) -> bool:
    if len(prompt) > 240 and any(word in prompt.lower() for word in ["implement", "fix", "refactor", "test", "build"]):
        return True
    return bool(re.search(r"\b(implement|fix|debug|refactor|benchmark|fuzz|test)\b", prompt, re.IGNORECASE))


def _extract_tool_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or "")
    return ""


def _parse_stdin(stdin: str | None) -> dict[str, Any]:
    data = stdin if stdin is not None else sys.stdin.read()
    if not data.strip():
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {}

