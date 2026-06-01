from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .corpus import load_corpus_stats
from .hooks import run_hook
from .orchestrator import Orchestrator, changed_commands_from_config, commands_from_config, load_experts
from .state import HarnessState
from .verifier import Verifier


DEFAULT_CONFIG = """verify:
  timeout_sec: 180
  memory_mb: 4096
  commands:
    - name: unit
      run: python -m unittest discover -s tests
  changed_file_rules:
    "rsi_harness/**/*.py": ["python -m unittest discover -s tests"]
    "tests/**/*.py": ["python -m unittest discover -s tests"]

search:
  experts: 3
  rounds: 2
  worktree: true
  selector: score
  feedback_budget_chars: 12000
  experts_file: experts.yaml
"""


DEFAULT_EXPERTS = """experts:
  - id: codex-fast-0
    driver: codex
    command: codex exec --skip-git-repo-check --sandbox workspace-write
    prompt_variant: direct
  - id: claude-deep-0
    driver: claude
    command: claude -p
    prompt_variant: tests-first
  - id: opencode-review-0
    driver: opencode
    command: opencode run
    prompt_variant: adversarial-review
"""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as exc:  # surface failures as structured output, not a traceback
        return _report_error(args, exc)


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        return cmd_init(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "task":
        return cmd_task(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "select":
        return cmd_select(args)
    if args.command == "learn":
        return cmd_learn(args)
    if args.command == "hook":
        return run_hook(args.event)
    build_parser().print_help()
    return 2


def _emit_json(command: str, data: object) -> None:
    print(json.dumps({"ok": True, "command": command, "data": data}, indent=2))


def _report_error(args: argparse.Namespace, exc: Exception) -> int:
    message = str(exc) or exc.__class__.__name__
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": getattr(args, "command", None),
                    "error": {"type": type(exc).__name__, "message": message},
                },
                indent=2,
            )
        )
    else:
        print(f"error: {message}", file=sys.stderr)
    # Exit 2 marks an internal/usage error, distinct from a clean "verification did
    # not pass" (exit 1), so callers gating on the exit code can tell them apart.
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rsi", description="Recursive self-improvement coding harness")
    parser.add_argument("--version", action="version", version=f"rsi-harness {__version__}")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Write starter .rsi.yaml and experts.yaml files")
    init.add_argument("--force", action="store_true", help="Overwrite existing files")

    verify = sub.add_parser("verify", help="Run configured verification commands")
    verify.add_argument("--config", default=".rsi.yaml")
    verify.add_argument("--changed", action="store_true", help="Use changed_file_rules when possible")
    verify.add_argument("--json", action="store_true", help="Emit JSON")

    task = sub.add_parser("task", help="Create an RSI task record")
    task.add_argument("spec", nargs="+")
    task.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Run batch candidate search")
    run.add_argument("--task", required=True, help="Task text or path to task markdown")
    run.add_argument("--config", default=".rsi.yaml")
    run.add_argument("--experts", default=None, help="Experts YAML path")
    run.add_argument("--rounds", type=int, default=None)
    run.add_argument("--dry-run", action="store_true", help="Create prompts without invoking external agents")
    run.add_argument("--json", action="store_true")

    select = sub.add_parser("select", help="Print selection for a task")
    select.add_argument("--task", default="latest")
    select.add_argument("--json", action="store_true")

    learn = sub.add_parser("learn", help="Summarize learnings from the .rsi/tasks corpus")
    learn.add_argument("--json", action="store_true")

    hook = sub.add_parser("hook", help="Run a Codex/Claude lifecycle hook")
    hook.add_argument("event", choices=["session-start", "prompt-submit", "pre-tool", "post-tool", "stop"])
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    targets = [(Path(".rsi.yaml"), DEFAULT_CONFIG), (Path("experts.yaml"), DEFAULT_EXPERTS)]
    if not args.force:
        existing = [str(path) for path, _ in targets if path.exists()]
        if existing:
            print(f"{', '.join(existing)} already exist; use --force to overwrite", file=sys.stderr)
            return 1
    for path, content in targets:
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    commands = changed_commands_from_config(config, Path.cwd()) if args.changed else commands_from_config(config)
    report = Verifier(timeout_sec=config.verify.timeout_sec).run(commands, Path.cwd())
    if args.json:
        _emit_json("verify", report.to_dict())
    else:
        _print_report(report.to_dict())
    return 0 if report.hard_pass else 1


def cmd_task(args: argparse.Namespace) -> int:
    spec = " ".join(args.spec)
    task = HarnessState().create_task(spec)
    if args.json:
        _emit_json("task", task.__dict__)
    else:
        print(task.path)
    return 0


def _stderr_progress(round_index: int, rounds: int, expert_id: str) -> None:
    print(f"rsi: round {round_index}/{rounds} expert {expert_id}", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    rounds = args.rounds if args.rounds is not None else config.search.rounds
    experts_path = Path(args.experts or config.search.experts_file)
    experts = load_experts(experts_path, fallback_count=config.search.experts)
    task_spec = _read_task_arg(args.task)
    selection = Orchestrator(config).run(
        task_spec, experts, rounds, Path.cwd(), dry_run=args.dry_run, on_progress=_stderr_progress
    )
    if args.json:
        _emit_json("run", selection)
    else:
        print(f"task={selection['task_id']}")
        winner = selection.get("winner")
        print(f"winner={winner['candidate_id']}" if winner else "winner=none (no candidates)")
        print(f"hard_pass={selection.get('hard_pass', False)}")
    return 0 if selection.get("hard_pass") else 1


def cmd_select(args: argparse.Namespace) -> int:
    state = HarnessState()
    task_id = state.latest_task_id() if args.task == "latest" else args.task
    selection_path = state.task_dir(task_id) / "selection.json"
    if not selection_path.exists():
        print(f"No selection.json for task {task_id}", file=sys.stderr)
        return 1
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    if args.json:
        _emit_json("select", payload)
    else:
        winner = payload.get("winner")
        if winner:
            print(f"winner={winner['candidate_id']}")
            print(f"score={winner['score']}")
        else:
            print("winner=none (no candidates)")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    stats = load_corpus_stats(HarnessState())
    if args.json:
        _emit_json("learn", stats.to_dict())
    else:
        print(f"tasks={stats.task_count} candidates={stats.candidate_count}")
        for expert in sorted(stats.expert_total_counts):
            total = stats.expert_total_counts[expert]
            passes = stats.expert_hard_pass_counts.get(expert, 0)
            print(f"{expert}: win_rate={stats.expert_win_rate(expert):.2f} hard_pass={passes}/{total}")
        for name, count in stats.top_failing_commands():
            print(f"failing: {name} x{count}")
    return 0


def _read_task_arg(value: str) -> str:
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return value


def _print_report(report: dict) -> None:
    status = "PASS" if report["hard_pass"] else "FAIL"
    print(f"{status} {report['runtime_sec']:.2f}s {report['output_bucket']}")
    for result in report["results"]:
        print(f"[{result['exit_code']}] {result['name']}: {result['command']}")


if __name__ == "__main__":
    raise SystemExit(main())

