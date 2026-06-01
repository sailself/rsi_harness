import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi_harness.adapters import DriverRun, DriverSpec, build_driver
from rsi_harness.config import (
    ConfigError,
    HarnessConfig,
    SearchConfig,
    VerifyCommandConfig,
    VerifyConfig,
    load_config,
)
from rsi_harness.hooks import run_hook
from rsi_harness.mcp_server import handle_request, process_line
from rsi_harness.orchestrator import ExpertSpec, Orchestrator
from rsi_harness.selector import CandidateScore, select_winner
from rsi_harness.state import HarnessState
from rsi_harness.verifier import VerificationCommand, Verifier


def _yaml_scalar(text: str) -> str:
    """Render text as a single-quoted YAML scalar (backslashes literal)."""
    return "'" + text.replace("'", "''") + "'"


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd())
    return subprocess.run(
        [sys.executable, "-m", "rsi_harness.cli", *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )


def _write_run_project(root: Path, run_cmd: str, *, rounds: int = 1) -> None:
    (root / ".rsi.yaml").write_text(
        "verify:\n"
        "  timeout_sec: 30\n"
        "  commands:\n"
        "    - name: gate\n"
        f"      run: {_yaml_scalar(run_cmd)}\n"
        "search:\n"
        f"  rounds: {rounds}\n"
        "  experts: 1\n"
        "  worktree: false\n",
        encoding="utf-8",
    )
    (root / "experts.yaml").write_text(
        "experts:\n  - id: codex-fast-0\n    driver: codex\n    prompt_variant: direct\n",
        encoding="utf-8",
    )


def _git_init_repo(root: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    (root / "sol.txt").write_text("base\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "sol.txt"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=root, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class _AppendingDriver:
    """Fake agent that appends a marker line to the tracked sol.txt in its cwd."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def build_argv(self, prompt: str) -> list[str]:
        return ["fake-agent", self.marker]

    def run(self, prompt: str, cwd: Path) -> DriverRun:
        sol = Path(cwd) / "sol.txt"
        existing = sol.read_text(encoding="utf-8") if sol.exists() else ""
        sol.write_text(existing + self.marker + "\n", encoding="utf-8")
        return DriverRun(argv=["fake-agent"], exit_code=0, stdout="", stderr="")


class OrchestratorIsolationTests(unittest.TestCase):
    def test_worktree_isolation_keeps_candidate_patches_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init_repo(root)
            config = HarnessConfig(
                verify=VerifyConfig(
                    commands=[VerifyCommandConfig(name="noop", run=f'"{sys.executable}" -c "pass"')]
                ),
                search=SearchConfig(worktree=True, rounds=2, experts=1),
            )
            counter = {"n": 0}

            def factory(spec: DriverSpec) -> _AppendingDriver:
                counter["n"] += 1
                return _AppendingDriver(f"candidate-{counter['n']}")

            orch = Orchestrator(config, state=HarnessState(root / ".rsi"), driver_factory=factory)
            orch.run("spec", [ExpertSpec(expert_id="e0", driver="codex")], rounds=2, cwd=root, dry_run=False)

            candidate_dirs = sorted((root / ".rsi" / "tasks").glob("*/candidates/*"))
            self.assertEqual(len(candidate_dirs), 2)
            second_patch = (candidate_dirs[-1] / "candidate.patch").read_text(encoding="utf-8")
            self.assertIn("candidate-2", second_patch)
            self.assertNotIn("candidate-1", second_patch)  # not accumulated from round 1
            # The real working tree was never mutated by the agents.
            self.assertEqual((root / "sol.txt").read_text(encoding="utf-8"), "base\n")

    def test_worktree_enabled_without_git_falls_back_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # deliberately NOT a git repo
            config = HarnessConfig(search=SearchConfig(worktree=True, rounds=1, experts=1))
            orch = Orchestrator(
                config,
                state=HarnessState(root / ".rsi"),
                driver_factory=lambda spec: _AppendingDriver("x"),
            )
            selection = orch.run(
                "spec", [ExpertSpec(expert_id="e0", driver="codex")], rounds=1, cwd=root, dry_run=True
            )
            self.assertEqual(selection["candidate_count"], 1)

    def test_worktree_add_failure_warns_about_degraded_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init_repo(root)  # baseline exists, so isolation is attempted
            config = HarnessConfig(search=SearchConfig(worktree=True, rounds=1, experts=1))
            orch = Orchestrator(
                config,
                state=HarnessState(root / ".rsi"),
                driver_factory=lambda spec: _AppendingDriver("x"),
            )
            buf = io.StringIO()
            with mock.patch("rsi_harness.orchestrator._git_worktree_add", return_value=False), contextlib.redirect_stderr(buf):
                orch.run("spec", [ExpertSpec(expert_id="e0", driver="codex")], rounds=1, cwd=root, dry_run=True)
            self.assertIn("worktree", buf.getvalue().lower())


class ConfigTests(unittest.TestCase):
    def test_loads_minimal_yaml_without_external_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rsi.yaml"
            path.write_text(
                """
verify:
  timeout_sec: 17
  commands:
    - name: unit
      run: python -m unittest
search:
  experts: 3
  rounds: 2
""".strip(),
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertEqual(config.verify.timeout_sec, 17)
        self.assertEqual(config.verify.commands[0].name, "unit")
        self.assertEqual(config.verify.commands[0].run, "python -m unittest")
        self.assertEqual(config.search.experts, 3)
        self.assertEqual(config.search.rounds, 2)


class VerifierTests(unittest.TestCase):
    def test_runs_commands_and_records_pass_fail_output_and_bucket(self) -> None:
        verifier = Verifier(timeout_sec=30)
        commands = [
            VerificationCommand(
                name="pass",
                run=f'"{sys.executable}" -c "print(\\"same behavior\\")"',
            ),
            VerificationCommand(
                name="fail",
                run=f'"{sys.executable}" -c "import sys; print(\\"bad\\"); sys.exit(3)"',
            ),
        ]

        report = verifier.run(commands, cwd=Path.cwd())

        self.assertFalse(report.hard_pass)
        self.assertEqual([result.name for result in report.results], ["pass", "fail"])
        self.assertEqual(report.results[0].exit_code, 0)
        self.assertEqual(report.results[1].exit_code, 3)
        self.assertIn("same behavior", report.results[0].stdout)
        self.assertTrue(report.output_bucket.startswith("sha256:"))
        self.assertGreater(report.runtime_sec, 0)


class StateTests(unittest.TestCase):
    def test_persists_task_candidate_and_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            task = state.create_task("make tests pass", {"acceptance": ["unit tests"]})
            candidate = state.create_candidate(
                task.task_id,
                expert_id="codex-fast-0",
                round_index=1,
                driver="codex",
                patch_text="diff --git a/x b/x\n",
            )
            state.write_candidate_report(
                task.task_id,
                candidate.candidate_id,
                {
                    "hard_pass": True,
                    "score": 1000,
                    "commands": ["unit"],
                },
            )

            task_doc = json.loads((state.task_dir(task.task_id) / "task.json").read_text())
            candidate_doc = json.loads(Path(candidate.metadata_path).read_text())
            report_doc = json.loads(
                (state.task_dir(task.task_id) / "candidates" / candidate.candidate_id / "report.json").read_text()
            )

        self.assertEqual(task_doc["spec"], "make tests pass")
        self.assertEqual(candidate_doc["expert_id"], "codex-fast-0")
        self.assertEqual(report_doc["commands"], ["unit"])


class SelectorTests(unittest.TestCase):
    def test_prefers_hard_pass_with_behavioral_vote_and_lower_risk(self) -> None:
        winner = select_winner(
            [
                CandidateScore(
                    candidate_id="passing-minimal",
                    hard_pass=True,
                    test_pass_rate=1.0,
                    generated_test_pass_rate=0.8,
                    behavioral_vote_count=2,
                    static_quality_score=0.9,
                    risk_score=0.1,
                    normalized_runtime=0.2,
                    patch_size_penalty=0.1,
                ),
                CandidateScore(
                    candidate_id="passing-risky",
                    hard_pass=True,
                    test_pass_rate=1.0,
                    generated_test_pass_rate=0.8,
                    behavioral_vote_count=2,
                    static_quality_score=0.9,
                    risk_score=0.9,
                    normalized_runtime=0.2,
                    patch_size_penalty=0.1,
                ),
                CandidateScore(
                    candidate_id="failing",
                    hard_pass=False,
                    test_pass_rate=1.0,
                    generated_test_pass_rate=1.0,
                    behavioral_vote_count=9,
                    static_quality_score=1.0,
                    risk_score=0.0,
                    normalized_runtime=0.0,
                    patch_size_penalty=0.0,
                ),
            ]
        )

        self.assertEqual(winner.candidate_id, "passing-minimal")
        self.assertGreater(winner.score, 1000)


class AdapterTests(unittest.TestCase):
    def test_builds_agent_driver_commands_for_codex_claude_and_opencode(self) -> None:
        cases = {
            "codex": ["codex", "exec"],
            "claude": ["claude", "-p"],
            "opencode": ["opencode", "run"],
        }
        for driver_name, expected_prefix in cases.items():
            with self.subTest(driver=driver_name):
                driver = build_driver(
                    DriverSpec(
                        driver=driver_name,
                        command=" ".join(expected_prefix),
                        extra_args=["--model", "test/model"] if driver_name == "opencode" else [],
                    )
                )
                argv = driver.build_argv("fix the bug")

                self.assertEqual(argv[: len(expected_prefix)], expected_prefix)
                self.assertIn("fix the bug", argv)


class CliTests(unittest.TestCase):
    def test_cli_verify_json_uses_configured_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Build a VALID single-quoted YAML scalar: backslashes (Windows exe
            # paths) are literal in single-quoted YAML, and embedded single
            # quotes are doubled. A double-quoted scalar would treat "\U..." in
            # a Windows path as an invalid escape and (correctly) fail to parse.
            run_cmd = f'"{sys.executable}" -c "print(\'ok\')"'
            yaml_run = "'" + run_cmd.replace("'", "''") + "'"
            (root / ".rsi.yaml").write_text(
                "verify:\n"
                "  timeout_sec: 30\n"
                "  commands:\n"
                "    - name: smoke\n"
                f"      run: {yaml_run}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path.cwd())

            result = subprocess.run(
                [sys.executable, "-m", "rsi_harness.cli", "verify", "--json"],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["hard_pass"])
        self.assertEqual(payload["results"][0]["name"], "smoke")


class PackagingTests(unittest.TestCase):
    def test_plugin_manifests_hooks_mcp_and_agent_instructions_are_present(self) -> None:
        root = Path.cwd()
        codex_manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        claude_manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        mcp = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        skill = (root / "skills" / "rsi-coding" / "SKILL.md").read_text(encoding="utf-8")
        agents = (root / "AGENTS.md").read_text(encoding="utf-8")

        self.assertEqual(codex_manifest["name"], "rsi-harness")
        self.assertEqual(claude_manifest["name"], "rsi-harness")
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("PreToolUse", hooks["hooks"])
        self.assertIn("rsi-harness", mcp["mcp_servers"])
        self.assertIn("rsi verify --changed --json", skill)
        self.assertIn("OpenCode", agents)


def _capture_hook(event: str, stdin: str | None = None, cwd: Path | None = None) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_hook(event, stdin=stdin, cwd=cwd)
    return rc, buf.getvalue()


class McpServerTests(unittest.TestCase):
    def test_unknown_method_returns_method_not_found(self) -> None:
        resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "bogus"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_list_returns_three_tools(self) -> None:
        resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(len(resp["result"]["tools"]), 3)

    def test_malformed_line_returns_parse_error_and_blank_is_ignored(self) -> None:
        resp = process_line("this is not json\n")
        payload = json.loads(resp)
        self.assertEqual(payload["error"]["code"], -32700)
        self.assertIsNone(process_line("   \n"))

    def test_select_winner_rejects_path_traversal_task_id(self) -> None:
        resp = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "rsi.select_winner", "arguments": {"task_id": "../../etc/passwd"}},
            }
        )
        self.assertIn("error", resp)
        self.assertIn("invalid task_id", resp["error"]["message"])


class HookEventLabelTests(unittest.TestCase):
    def test_session_start_emits_session_start_event(self) -> None:
        _rc, out = _capture_hook("session-start", stdin="{}")
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_prompt_submit_emits_user_prompt_submit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _rc, out = _capture_hook(
                "prompt-submit",
                stdin=json.dumps({"prompt": "please implement and fix the parser"}),
                cwd=Path(tmp),
            )
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_post_tool_failure_emits_post_tool_use_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failing = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
            (root / ".rsi.yaml").write_text(
                "verify:\n  timeout_sec: 30\n  commands:\n    - name: gate\n"
                f"      run: {_yaml_scalar(failing)}\n",
                encoding="utf-8",
            )
            _rc, out = _capture_hook("post-tool", stdin="{}", cwd=root)
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_stop_with_failing_report_emits_system_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback = root / ".rsi" / "hook-feedback"
            feedback.mkdir(parents=True)
            (feedback / "latest.json").write_text(json.dumps({"hard_pass": False}), encoding="utf-8")
            _rc, out = _capture_hook("stop", stdin="{}", cwd=root)
        payload = json.loads(out)
        self.assertIn("systemMessage", payload)

    def test_pre_tool_blocks_destructive_command(self) -> None:
        _rc, out = _capture_hook(
            "pre-tool",
            stdin=json.dumps({"tool_input": {"command": "git reset --hard HEAD~3"}}),
        )
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pre_tool_allows_benign_command(self) -> None:
        _rc, out = _capture_hook(
            "pre-tool",
            stdin=json.dumps({"tool_input": {"command": "python -m unittest"}}),
        )
        self.assertEqual(out.strip(), "")


class CliInitTests(unittest.TestCase):
    def test_init_writes_python_config_with_score_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = _run_cli(root, "init")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(root / ".rsi.yaml")
            self.assertEqual(config.verify.commands[0].name, "unit")
            self.assertIn("unittest", config.verify.commands[0].run)
            self.assertEqual(config.search.selector, "score")
            self.assertTrue(
                any(pattern.endswith(".py") for pattern in config.verify.changed_file_rules),
                config.verify.changed_file_rules,
            )

    def test_init_is_atomic_when_a_target_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "experts.yaml").write_text("experts: []\n", encoding="utf-8")
            result = _run_cli(root, "init")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / ".rsi.yaml").exists())  # no partial write


class AgentDriverResilienceTests(unittest.TestCase):
    def test_missing_binary_returns_failed_run_not_raises(self) -> None:
        driver = build_driver(DriverSpec(driver="codex", command="rsi-no-such-binary-xyz"))
        run = driver.run("do the thing", cwd=Path.cwd())
        self.assertNotEqual(run.exit_code, 0)
        self.assertTrue(run.stderr)

    def test_timeout_returns_failed_run_not_raises(self) -> None:
        # Pass argv tokens via extra_args so shlex quote handling (which differs
        # across platforms) cannot corrupt the executable/code arguments.
        driver = build_driver(
            DriverSpec(
                driver="codex",
                command=sys.executable,
                extra_args=["-c", "import time; time.sleep(5)"],
                timeout_sec=1,
            )
        )
        run = driver.run("x", cwd=Path.cwd())
        self.assertNotEqual(run.exit_code, 0)
        self.assertIn("timed out", run.stderr.lower())


class CliRunExitCodeTests(unittest.TestCase):
    def test_run_exits_zero_when_winner_hard_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            result = _run_cli(root, "run", "--task", "make it pass", "--rounds", "1", "--dry-run", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["hard_pass"])
            self.assertIsNotNone(payload["winner"])

    def test_run_exits_nonzero_when_no_candidate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "import sys; sys.exit(1)"')
            result = _run_cli(root, "run", "--task", "x", "--rounds", "1", "--dry-run", "--json")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["hard_pass"])

    def test_run_with_zero_rounds_produces_no_candidates_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            result = _run_cli(root, "run", "--task", "x", "--rounds", "0", "--dry-run", "--json")
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["candidate_count"], 0)
            self.assertIsNone(payload["winner"])
            self.assertFalse(payload["hard_pass"])


class CliErrorEnvelopeTests(unittest.TestCase):
    def test_select_missing_task_emits_json_error_with_exit_code_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli(Path(tmp), "select", "--task", "latest", "--json")
            # Exit 2 distinguishes an internal error from a legitimate non-pass (exit 1).
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("error", payload)


class ConfigErrorTests(unittest.TestCase):
    def test_malformed_yaml_raises_config_error(self) -> None:
        try:
            import yaml  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PyYAML not installed; the minimal parser does not validate syntax")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rsi.yaml"
            path.write_text("verify:\n  commands: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_command_missing_run_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rsi.yaml"
            path.write_text("verify:\n  commands:\n    - name: x\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_numeric_timeout_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rsi.yaml"
            path.write_text("verify:\n  timeout_sec: not-a-number\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class VerifierTimeoutTests(unittest.TestCase):
    def test_explicit_zero_timeout_is_honored_not_default(self) -> None:
        verifier = Verifier(timeout_sec=30)
        command = VerificationCommand(
            name="slow",
            run=f'"{sys.executable}" -c "import time; time.sleep(1)"',
            timeout_sec=0,
        )
        report = verifier.run([command], cwd=Path.cwd())
        self.assertTrue(report.results[0].timed_out)

    def test_command_exceeding_timeout_is_recorded(self) -> None:
        verifier = Verifier(timeout_sec=30)
        command = VerificationCommand(
            name="hang",
            run=f'"{sys.executable}" -c "import time; time.sleep(5)"',
            timeout_sec=1,
        )
        report = verifier.run([command], cwd=Path.cwd())
        result = report.results[0]
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, -1)
        self.assertIn("Timed out", result.stderr)
        self.assertFalse(report.hard_pass)

    def test_empty_command_list_is_not_a_hard_pass(self) -> None:
        report = Verifier().run([], cwd=Path.cwd())
        self.assertFalse(report.hard_pass)


if __name__ == "__main__":
    unittest.main()
