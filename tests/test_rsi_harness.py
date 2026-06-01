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
from rsi_harness.corpus import CorpusStats, load_corpus_stats
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
from rsi_harness.orchestrator import (
    ExpertSpec,
    Orchestrator,
    _parse_test_counts,
    build_candidate_prompt,
    commands_from_config,
    git_changed_files,
    score_from_report,
)
from rsi_harness.selector import CandidateScore, score_candidate, select_winner
from rsi_harness.state import HarnessState
from rsi_harness.verifier import (
    CommandResult,
    VerificationCommand,
    VerificationReport,
    Verifier,
    _output_bucket,
)


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


class _MarkerDriver:
    """Fake agent that writes its marker to a tracked-by-cwd file the verifier reads back."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def build_argv(self, prompt: str) -> list[str]:
        return ["fake-agent", self.marker]

    def run(self, prompt: str, cwd: Path) -> DriverRun:
        (Path(cwd) / "marker.txt").write_text(self.marker, encoding="utf-8")
        return DriverRun(argv=["fake-agent"], exit_code=0, stdout="", stderr="")


class ChangedFileScopingTests(unittest.TestCase):
    def test_git_changed_files_includes_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init_repo(root)
            (root / "new_module.py").write_text("x = 1\n", encoding="utf-8")  # untracked
            self.assertIn("new_module.py", git_changed_files(root))

    def test_git_changed_files_excludes_rsi_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init_repo(root)
            (root / "src.py").write_text("x = 1\n", encoding="utf-8")
            (root / ".rsi" / "tasks").mkdir(parents=True)
            (root / ".rsi" / "tasks" / "t.json").write_text("{}", encoding="utf-8")
            files = git_changed_files(root)
            self.assertIn("src.py", files)
            self.assertFalse(any(f.startswith(".rsi/") for f in files), files)

    def test_changed_only_routes_search_through_changed_file_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git_init_repo(root)
            rule_cmd = f'"{sys.executable}" -c "print(\'RULE\')"'
            full_cmd = f'"{sys.executable}" -c "pass"'
            config = HarnessConfig(
                verify=VerifyConfig(
                    commands=[VerifyCommandConfig(name="full", run=full_cmd)],
                    changed_file_rules={"*.txt": [rule_cmd]},
                ),
                search=SearchConfig(worktree=False, rounds=1, experts=1, changed_only=True),
            )
            # the fake agent writes marker.txt (untracked .txt) -> matches the *.txt rule
            orch = Orchestrator(
                config,
                state=HarnessState(root / ".rsi"),
                driver_factory=lambda spec: _MarkerDriver("changed"),
            )
            orch.run("spec", [ExpertSpec(expert_id="e", driver="codex")], rounds=1, cwd=root, dry_run=False)
            cand = sorted((root / ".rsi" / "tasks").glob("*/candidates/*"))[0]
            report = json.loads((cand / "report.json").read_text())
            commands = [r["command"] for r in report["results"]]
            self.assertTrue(any("RULE" in c for c in commands), commands)


class AgentTimeoutConfigTests(unittest.TestCase):
    def test_agent_timeout_sec_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rsi.yaml"
            path.write_text("search:\n  agent_timeout_sec: 42\n", encoding="utf-8")
            self.assertEqual(load_config(path).search.agent_timeout_sec, 42)

    def test_agent_timeout_is_threaded_into_driver_spec(self) -> None:
        captured = {}

        def factory(spec: DriverSpec):
            captured["timeout"] = spec.timeout_sec
            return _MarkerDriver("x")

        config = HarnessConfig(search=SearchConfig(agent_timeout_sec=42))
        with tempfile.TemporaryDirectory() as tmp:
            orch = Orchestrator(config, state=HarnessState(Path(tmp) / ".rsi"), driver_factory=factory)
            orch._run_expert(ExpertSpec(expert_id="e", driver="codex"), "prompt", Path(tmp), dry_run=True)
        self.assertEqual(captured["timeout"], 42)


class PerLineageFeedbackTests(unittest.TestCase):
    def test_round2_feedback_is_per_expert_not_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # verify prints the marker the candidate's agent wrote, then fails, so the
            # marker shows up in that candidate's compacted feedback.
            run_cmd = f'"{sys.executable}" -c "print(open(\'marker.txt\').read()); import sys; sys.exit(1)"'
            config = HarnessConfig(
                verify=VerifyConfig(commands=[VerifyCommandConfig(name="gate", run=run_cmd)]),
                search=SearchConfig(worktree=False, rounds=2, experts=2),
            )
            experts = [
                ExpertSpec(expert_id="expA", driver="codex", command="ALPHA"),
                ExpertSpec(expert_id="expB", driver="codex", command="BETA"),
            ]
            orch = Orchestrator(
                config,
                state=HarnessState(root / ".rsi"),
                driver_factory=lambda spec: _MarkerDriver(spec.command or ""),
            )
            orch.run("spec", experts, rounds=2, cwd=root, dry_run=False)

            prompts = {}
            for cand_dir in sorted((root / ".rsi" / "tasks").glob("*/candidates/*")):
                meta = json.loads((cand_dir / "candidate.json").read_text())
                prompts[(meta["expert_id"], meta["round_index"])] = (cand_dir / "prompt.md").read_text()

            round2_expert_a = prompts[("expA", 2)]
            self.assertIn("ALPHA", round2_expert_a)  # expert A's own round-1 failure
            self.assertNotIn("BETA", round2_expert_a)  # not the sibling's last result


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


class PromptVariantTests(unittest.TestCase):
    def test_variants_produce_distinct_strategy_text(self) -> None:
        direct = build_candidate_prompt("do the thing", "direct")
        tests_first = build_candidate_prompt("do the thing", "tests-first")
        adversarial = build_candidate_prompt("do the thing", "adversarial-review")
        self.assertIn("failing test", tests_first.lower())
        self.assertIn("critique", adversarial.lower())
        self.assertNotEqual(direct, tests_first)
        self.assertNotEqual(tests_first, adversarial)

    def test_unknown_variant_falls_back_to_label(self) -> None:
        prompt = build_candidate_prompt("x", "some-custom-variant")
        self.assertIn("some-custom-variant", prompt)

    def test_feedback_is_appended_when_present(self) -> None:
        prompt = build_candidate_prompt("x", "direct", "the build failed: missing import")
        self.assertIn("missing import", prompt)

    def test_corpus_hints_section_is_injected(self) -> None:
        prompt = build_candidate_prompt("x", "direct", corpus_hints="check FOO fails often")
        self.assertIn("check FOO fails often", prompt)
        self.assertIn("Lessons from prior runs", prompt)


class CorpusFeedbackTests(unittest.TestCase):
    @staticmethod
    def _seed(state: HarnessState, expert_id: str, failing_command: str | None = None) -> None:
        prior = state.create_task("prior task")
        cand = state.create_candidate(prior.task_id, expert_id, 1, "codex", "diff --git a/x b/x\n")
        results = [{"name": failing_command, "exit_code": 1, "timed_out": False}] if failing_command else []
        hard = failing_command is None
        state.write_candidate_report(prior.task_id, cand.candidate_id, {"hard_pass": hard, "results": results})
        if hard:
            state.write_task_artifact(
                prior.task_id, "selection.json", json.dumps({"winner": {"candidate_id": cand.candidate_id}})
            )

    def test_use_corpus_orders_experts_by_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            self._seed(state, "expB")  # expB has a prior win; expA has no history
            config = HarnessConfig(search=SearchConfig(use_corpus=True, rounds=1, experts=2, worktree=False))
            orch = Orchestrator(config, state=state, driver_factory=lambda spec: _MarkerDriver("m"))
            orch.run(
                "new", [ExpertSpec("expA", "codex"), ExpertSpec("expB", "claude")], rounds=1, cwd=Path(tmp), dry_run=True
            )
            meta = json.loads((state.task_dir(state.latest_task_id()) / "task.json").read_text())["metadata"]
            self.assertEqual(meta["experts"], ["expB", "expA"])

    def test_use_corpus_injects_failure_hints_into_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            self._seed(state, "expA", failing_command="flaky_integration")
            config = HarnessConfig(search=SearchConfig(use_corpus=True, rounds=1, experts=1, worktree=False))
            orch = Orchestrator(config, state=state, driver_factory=lambda spec: _MarkerDriver("m"))
            orch.run("new", [ExpertSpec("expA", "codex")], rounds=1, cwd=Path(tmp), dry_run=True)
            cand_dir = sorted((state.task_dir(state.latest_task_id()) / "candidates").iterdir())[0]
            self.assertIn("flaky_integration", (cand_dir / "prompt.md").read_text())

    def test_default_run_does_not_read_corpus(self) -> None:
        # use_corpus defaults False: experts keep their given order, no hints.
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            self._seed(state, "expB")
            config = HarnessConfig(search=SearchConfig(rounds=1, experts=2, worktree=False))
            orch = Orchestrator(config, state=state, driver_factory=lambda spec: _MarkerDriver("m"))
            orch.run(
                "new", [ExpertSpec("expA", "codex"), ExpertSpec("expB", "claude")], rounds=1, cwd=Path(tmp), dry_run=True
            )
            meta = json.loads((state.task_dir(state.latest_task_id()) / "task.json").read_text())["metadata"]
            self.assertEqual(meta["experts"], ["expA", "expB"])


class BehavioralVotingTests(unittest.TestCase):
    def test_behavioral_vote_prefers_larger_agreeing_cluster(self) -> None:
        scores = [
            CandidateScore(candidate_id="a", hard_pass=True, output_bucket="X"),
            CandidateScore(candidate_id="b", hard_pass=True, output_bucket="X"),
            CandidateScore(candidate_id="c", hard_pass=True, output_bucket="Y"),
        ]
        winner = select_winner(scores, selector="behavioral_vote")
        self.assertIn(winner.candidate_id, {"a", "b"})  # the 2-agreement cluster beats the lone one
        self.assertAlmostEqual(winner.behavioral_vote_count, 2 / 3)  # normalized share, not raw count

    def test_score_selector_leaves_vote_count_at_default(self) -> None:
        scores = [
            CandidateScore(candidate_id="a", hard_pass=True, output_bucket="X", test_pass_rate=1.0),
            CandidateScore(candidate_id="b", hard_pass=True, output_bucket="Y", test_pass_rate=1.0),
        ]
        winner = select_winner(scores, selector="score")
        self.assertEqual(winner.behavioral_vote_count, 1)


class TieBreakTests(unittest.TestCase):
    def test_tie_break_uses_merit_not_largest_candidate_id(self) -> None:
        # Constructed so both score exactly 1070, but "aaa" has the higher test_pass_rate.
        a = CandidateScore(candidate_id="aaa", hard_pass=True, test_pass_rate=1.0, patch_size_penalty=8.0)
        b = CandidateScore(candidate_id="zzz", hard_pass=True, test_pass_rate=0.5, patch_size_penalty=0.0)
        self.assertAlmostEqual(score_candidate(a).score, score_candidate(b).score)
        winner = select_winner([a, b])
        self.assertEqual(winner.candidate_id, "aaa")  # merit (test_pass_rate) wins, not the larger id


class ParseTestCountsTests(unittest.TestCase):
    def test_unittest_failures_and_errors(self) -> None:
        self.assertEqual(_parse_test_counts("Ran 10 tests in 0.5s\n\nFAILED (failures=2, errors=1)"), (7, 10))

    def test_unittest_all_pass(self) -> None:
        self.assertEqual(_parse_test_counts("Ran 5 tests in 0.1s\n\nOK"), (5, 5))

    def test_unittest_skips_are_neutral_not_counted_as_passed(self) -> None:
        # 5 ran, 3 skipped, 0 failed -> 2 of 2 countable passed (skips excluded)
        self.assertEqual(_parse_test_counts("Ran 5 tests in 0.1s\n\nOK (skipped=3)"), (2, 2))

    def test_pytest_framed_summary(self) -> None:
        text = "collected 10 items\n==== 3 failed, 7 passed in 0.4s ===="
        self.assertEqual(_parse_test_counts(text), (7, 10))

    def test_ignores_unframed_narrative_counts(self) -> None:
        # Not a test runner summary -> no signal (must not invent counts).
        self.assertIsNone(_parse_test_counts("Found 5 errors in the log file"))

    def test_does_not_let_narrative_pollute_pytest_summary(self) -> None:
        text = "2 failed lint rules earlier\n==== 3 failed, 7 passed in 0.4s ===="
        self.assertEqual(_parse_test_counts(text), (7, 10))


class CorpusStatsTests(unittest.TestCase):
    def test_load_corpus_stats_computes_wins_passes_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            task = state.create_task("t1")
            winner = state.create_candidate(task.task_id, "expA", 1, "codex", "diff --git a/x b/x\n")
            state.write_candidate_report(task.task_id, winner.candidate_id, {"hard_pass": True, "results": []})
            loser = state.create_candidate(task.task_id, "expB", 1, "claude", "diff --git a/y b/y\n")
            state.write_candidate_report(
                task.task_id,
                loser.candidate_id,
                {"hard_pass": False, "results": [{"name": "unit", "exit_code": 1, "timed_out": False}]},
            )
            state.write_task_artifact(
                task.task_id, "selection.json", json.dumps({"winner": {"candidate_id": winner.candidate_id}})
            )

            stats = load_corpus_stats(state)
            self.assertEqual(stats.task_count, 1)
            self.assertEqual(stats.candidate_count, 2)
            self.assertEqual(stats.expert_win_counts.get("expA"), 1)
            self.assertEqual(stats.expert_total_counts.get("expB"), 1)
            self.assertEqual(stats.expert_hard_pass_counts.get("expA"), 1)
            self.assertAlmostEqual(stats.expert_win_rate("expA"), 1.0)
            self.assertAlmostEqual(stats.expert_win_rate("expB"), 0.0)
            self.assertEqual(stats.failing_commands.get("unit"), 1)

    def test_runtime_exceeded_counts_as_a_failing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            task = state.create_task("t")
            cand = state.create_candidate(task.task_id, "expA", 1, "codex", "diff")
            state.write_candidate_report(
                task.task_id,
                cand.candidate_id,
                {
                    "hard_pass": False,
                    "results": [{"name": "slow_bench", "exit_code": 0, "timed_out": False, "runtime_exceeded": True}],
                },
            )
            self.assertEqual(load_corpus_stats(state).failing_commands.get("slow_bench"), 1)

    def test_candidate_without_expert_id_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = HarnessState(Path(tmp) / ".rsi")
            task = state.create_task("t")
            cand_dir = state.task_dir(task.task_id) / "candidates" / "bogus"
            cand_dir.mkdir(parents=True)
            (cand_dir / "candidate.json").write_text(json.dumps({"candidate_id": "bogus"}), encoding="utf-8")
            stats = load_corpus_stats(state)
            self.assertEqual(stats.candidate_count, 0)
            self.assertNotIn("", stats.expert_total_counts)

    def test_load_corpus_stats_on_empty_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stats = load_corpus_stats(HarnessState(Path(tmp) / ".rsi"))
            self.assertEqual(stats.task_count, 0)
            self.assertEqual(stats.expert_win_rate("anything"), 0.0)


class ScoreFromReportTests(unittest.TestCase):
    @staticmethod
    def _report(results: list, hard_pass: bool = True) -> VerificationReport:
        return VerificationReport(hard_pass=hard_pass, results=results, runtime_sec=0.0, output_bucket="b")

    def test_test_pass_rate_uses_parsed_unittest_counts(self) -> None:
        # unittest writes its summary to stderr; 7 of 10 passed.
        result = CommandResult(
            name="unit",
            command="python -m unittest",
            exit_code=1,
            stdout="",
            stderr="Ran 10 tests in 0.5s\n\nFAILED (failures=2, errors=1)",
            runtime_sec=0.5,
        )
        score = score_from_report("c", self._report([result], hard_pass=False), "diff --git a/x b/x\n+1\n")
        self.assertAlmostEqual(score.test_pass_rate, 0.7)

    def test_test_pass_rate_falls_back_to_command_level(self) -> None:
        passing = CommandResult(name="a", command="lint", exit_code=0, stdout="clean", stderr="", runtime_sec=0.0)
        failing = CommandResult(name="b", command="build", exit_code=1, stdout="boom", stderr="", runtime_sec=0.0)
        score = score_from_report("c", self._report([passing, failing], hard_pass=False), "")
        self.assertAlmostEqual(score.test_pass_rate, 0.5)

    def test_risk_score_increases_with_deletions(self) -> None:
        report = self._report([], hard_pass=True)
        few = score_from_report("a", report, "diff --git a/x b/x\n+added\n")
        many = score_from_report("b", report, "diff --git a/x b/x\n" + "-gone\n" * 60 + "+x\n")
        self.assertLess(few.risk_score, many.risk_score)

    def test_static_quality_rewards_real_diff_over_chatter(self) -> None:
        report = self._report([], hard_pass=True)
        diff = score_from_report("a", report, "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")
        chatter = score_from_report("b", report, "Sure! I updated the parser to handle the edge case.")
        self.assertGreater(diff.static_quality_score, chatter.static_quality_score)


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
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["hard_pass"])
        self.assertEqual(payload["data"]["results"][0]["name"], "smoke")


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

    def test_tool_names_use_underscores_to_match_surfaced_form(self) -> None:
        resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in resp["result"]["tools"]}
        self.assertEqual(names, {"rsi_create_task", "rsi_run_verify", "rsi_select_winner"})

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
                "params": {"name": "rsi_select_winner", "arguments": {"task_id": "../../etc/passwd"}},
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


class CliLearnTests(unittest.TestCase):
    def test_learn_json_summarizes_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            _run_cli(root, "run", "--task", "x", "--rounds", "1", "--dry-run", "--json")
            result = _run_cli(root, "learn", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "learn")
            self.assertGreaterEqual(payload["data"]["task_count"], 1)
            self.assertGreaterEqual(payload["data"]["candidate_count"], 1)


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
            data = json.loads(result.stdout)["data"]
            self.assertTrue(data["hard_pass"])
            self.assertIsNotNone(data["winner"])

    def test_run_exits_nonzero_when_no_candidate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "import sys; sys.exit(1)"')
            result = _run_cli(root, "run", "--task", "x", "--rounds", "1", "--dry-run", "--json")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse(json.loads(result.stdout)["data"]["hard_pass"])

    def test_run_with_zero_rounds_produces_no_candidates_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            result = _run_cli(root, "run", "--task", "x", "--rounds", "0", "--dry-run", "--json")
            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)["data"]
            self.assertEqual(data["candidate_count"], 0)
            self.assertIsNone(data["winner"])
            self.assertFalse(data["hard_pass"])


class JsonEnvelopeTests(unittest.TestCase):
    def test_verify_json_uses_ok_command_data_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            result = _run_cli(root, "verify", "--json")
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "verify")
            self.assertTrue(payload["data"]["hard_pass"])

    def test_run_json_envelope_and_stderr_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            result = _run_cli(root, "run", "--task", "x", "--rounds", "1", "--dry-run", "--json")
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "run")
            self.assertIn("candidate_count", payload["data"])
            self.assertIn("round", result.stderr.lower())  # progress to stderr


class CliErrorEnvelopeTests(unittest.TestCase):
    def test_select_missing_task_emits_json_error_with_exit_code_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_cli(Path(tmp), "select", "--task", "latest", "--json")
            # Exit 2 distinguishes an internal error from a legitimate non-pass (exit 1).
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("error", payload)

    def test_select_non_json_handles_null_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run_project(root, f'"{sys.executable}" -c "print(0)"')
            _run_cli(root, "run", "--task", "x", "--rounds", "0", "--dry-run", "--json")
            result = _run_cli(root, "select")  # non-JSON, winner is null
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("none", result.stdout.lower())


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


class OutputBucketTests(unittest.TestCase):
    @staticmethod
    def _result(stdout: str, exit_code: int = 0) -> CommandResult:
        return CommandResult(name="t", command="c", exit_code=exit_code, stdout=stdout, stderr="", runtime_sec=0.0)

    def test_bucket_is_stable_across_test_durations(self) -> None:
        a = _output_bucket([self._result("Ran 3 tests in 0.12s\n\nOK")])
        b = _output_bucket([self._result("Ran 3 tests in 9.87s\n\nOK")])
        self.assertEqual(a, b)

    def test_bucket_changes_on_real_output_difference(self) -> None:
        a = _output_bucket([self._result("Ran 3 tests in 0.12s\n\nOK")])
        b = _output_bucket([self._result("Ran 4 tests in 0.12s\n\nOK")])
        self.assertNotEqual(a, b)

    def test_bucket_strips_ansi_color(self) -> None:
        a = _output_bucket([self._result("\x1b[32mPASSED\x1b[0m")])
        b = _output_bucket([self._result("PASSED")])
        self.assertEqual(a, b)


class ConfigRuntimeGateTests(unittest.TestCase):
    def test_max_runtime_sec_is_parsed_and_threaded_to_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".rsi.yaml"
            path.write_text(
                "verify:\n  commands:\n    - name: unit\n      run: pytest\n      max_runtime_sec: 5\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.verify.commands[0].max_runtime_sec, 5)
            self.assertEqual(commands_from_config(config)[0].max_runtime_sec, 5)


class VerifierRuntimeGateTests(unittest.TestCase):
    def test_command_exceeding_max_runtime_is_excluded_from_hard_pass(self) -> None:
        command = VerificationCommand(
            name="slow-but-ok",
            run=f'"{sys.executable}" -c "import time; time.sleep(2)"',
            timeout_sec=30,
            max_runtime_sec=1,
        )
        report = Verifier().run([command], cwd=Path.cwd())
        self.assertEqual(report.results[0].exit_code, 0)  # it completed successfully
        self.assertTrue(report.results[0].runtime_exceeded)  # but it was too slow
        self.assertFalse(report.hard_pass)

    def test_fast_command_within_max_runtime_hard_passes(self) -> None:
        command = VerificationCommand(
            name="fast",
            run=f'"{sys.executable}" -c "pass"',
            timeout_sec=30,
            max_runtime_sec=30,
        )
        report = Verifier().run([command], cwd=Path.cwd())
        self.assertFalse(report.results[0].runtime_exceeded)
        self.assertTrue(report.hard_pass)


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
