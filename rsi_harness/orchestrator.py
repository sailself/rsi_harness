from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import DriverSpec, build_driver
from .config import HarnessConfig, _load_structured_data
from .selector import CandidateScore, select_winner
from .state import HarnessState
from .verifier import VerificationCommand, VerificationReport, Verifier


@dataclass(frozen=True)
class ExpertSpec:
    expert_id: str
    driver: str
    command: str | None = None
    prompt_variant: str = "direct"
    extra_args: list[str] = field(default_factory=list)


def load_experts(path: Path, fallback_count: int = 1) -> list[ExpertSpec]:
    if not path.exists():
        return [
            ExpertSpec(expert_id=f"codex-fast-{index}", driver="codex", prompt_variant="direct")
            for index in range(fallback_count)
        ]
    data = _load_structured_data(path.read_text(encoding="utf-8"), path.suffix)
    experts = []
    for item in data.get("experts", []):
        experts.append(
            ExpertSpec(
                expert_id=str(item.get("id") or item.get("expert_id")),
                driver=str(item["driver"]),
                command=item.get("command"),
                prompt_variant=str(item.get("prompt_variant", "direct")),
                extra_args=[str(arg) for arg in item.get("extra_args", [])],
            )
        )
    return experts


class Orchestrator:
    def __init__(self, config: HarnessConfig, state: HarnessState | None = None) -> None:
        self.config = config
        self.state = state or HarnessState()
        self.verifier = Verifier(timeout_sec=config.verify.timeout_sec)

    def run(
        self,
        task_spec: str,
        experts: list[ExpertSpec],
        rounds: int,
        cwd: Path,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        task = self.state.create_task(task_spec, {"rounds": rounds, "experts": [expert.expert_id for expert in experts]})
        feedback = ""
        scores: list[CandidateScore] = []
        for round_index in range(1, rounds + 1):
            for expert in experts:
                prompt = build_candidate_prompt(task_spec, expert.prompt_variant, feedback)
                candidate_output = self._run_expert(expert, prompt, cwd, dry_run)
                patch_text = capture_git_diff(cwd) or candidate_output["stdout"]
                candidate = self.state.create_candidate(
                    task.task_id,
                    expert_id=expert.expert_id,
                    round_index=round_index,
                    driver=expert.driver,
                    patch_text=patch_text,
                )
                candidate_dir = Path(candidate.metadata_path).parent
                (candidate_dir / "prompt.md").write_text(prompt, encoding="utf-8")
                (candidate_dir / "agent.json").write_text(json.dumps(candidate_output, indent=2), encoding="utf-8")

                report = self.verifier.run(commands_from_config(self.config), cwd)
                report_doc = report.to_dict()
                report_doc["argv"] = candidate_output.get("argv")
                report_doc["agent_exit_code"] = candidate_output.get("exit_code")
                self.state.write_candidate_report(task.task_id, candidate.candidate_id, report_doc)
                scores.append(score_from_report(candidate.candidate_id, report, patch_text))
                feedback = compact_feedback(report, self.config.search.feedback_budget_chars)

        winner = select_winner(scores)
        selection = {"task_id": task.task_id, "winner": winner.__dict__, "candidate_count": len(scores)}
        self.state.write_task_artifact(task.task_id, "selection.json", json.dumps(selection, indent=2))
        return selection

    def _run_expert(self, expert: ExpertSpec, prompt: str, cwd: Path, dry_run: bool) -> dict[str, Any]:
        spec = DriverSpec(
            driver=expert.driver,
            command=expert.command,
            extra_args=expert.extra_args,
        )
        driver = build_driver(spec)
        argv = driver.build_argv(prompt)
        if dry_run:
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": "", "dry_run": True}
        run = driver.run(prompt, cwd)
        return {"argv": run.argv, "exit_code": run.exit_code, "stdout": run.stdout, "stderr": run.stderr}


def commands_from_config(config: HarnessConfig) -> list[VerificationCommand]:
    return [
        VerificationCommand(name=command.name, run=command.run, timeout_sec=command.timeout_sec)
        for command in config.verify.commands
    ]


def changed_commands_from_config(config: HarnessConfig, cwd: Path) -> list[VerificationCommand]:
    changed_files = git_changed_files(cwd)
    commands: list[VerificationCommand] = []
    seen = set()
    for pattern, rule_commands in config.verify.changed_file_rules.items():
        if not any(fnmatch.fnmatch(path, pattern) for path in changed_files):
            continue
        for run in rule_commands:
            if run in seen:
                continue
            seen.add(run)
            commands.append(VerificationCommand(name=run, run=run))
    return commands or commands_from_config(config)


def git_changed_files(cwd: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]


def capture_git_diff(cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "diff", "--binary"],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def build_candidate_prompt(task_spec: str, prompt_variant: str, feedback: str = "") -> str:
    parts = [
        "# RSI candidate task",
        "",
        task_spec.strip(),
        "",
        "Produce a focused patch. Prefer tests first, run the configured verification, and explain residual risk.",
        f"Prompt variant: {prompt_variant}",
    ]
    if feedback:
        parts.extend(["", "## Previous executable feedback", feedback])
    return "\n".join(parts)


def compact_feedback(report: VerificationReport, budget_chars: int) -> str:
    chunks = []
    for result in report.results:
        if result.exit_code == 0 and not result.timed_out:
            continue
        output = (result.stderr or result.stdout).strip()
        chunks.append(f"$ {result.command}\nexit={result.exit_code}\n{output}")
    feedback = "\n\n".join(chunks) or "All configured checks passed."
    return feedback[:budget_chars]


def score_from_report(candidate_id: str, report: VerificationReport, patch_text: str) -> CandidateScore:
    pass_count = sum(1 for result in report.results if result.exit_code == 0 and not result.timed_out)
    total = len(report.results) or 1
    patch_penalty = min(len(patch_text) / 20000, 10)
    runtime_penalty = min(report.runtime_sec / 120, 10)
    return CandidateScore(
        candidate_id=candidate_id,
        hard_pass=report.hard_pass,
        test_pass_rate=pass_count / total,
        generated_test_pass_rate=0.0,
        behavioral_vote_count=1,
        static_quality_score=0.5 if patch_text else 0.0,
        risk_score=0.0 if report.hard_pass else 1.0,
        normalized_runtime=runtime_penalty,
        patch_size_penalty=patch_penalty,
    )

