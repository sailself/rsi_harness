import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rsi_harness.adapters import DriverSpec, build_driver
from rsi_harness.config import load_config
from rsi_harness.selector import CandidateScore, select_winner
from rsi_harness.state import HarnessState
from rsi_harness.verifier import VerificationCommand, Verifier


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
            (root / ".rsi.yaml").write_text(
                f"""
verify:
  timeout_sec: 30
  commands:
    - name: smoke
      run: "{sys.executable}" -c "print('ok')"
""".strip(),
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


if __name__ == "__main__":
    unittest.main()
