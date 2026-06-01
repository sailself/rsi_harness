from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

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
    def __init__(
        self,
        config: HarnessConfig,
        state: HarnessState | None = None,
        driver_factory: Callable[[DriverSpec], Any] = build_driver,
    ) -> None:
        self.config = config
        self.state = state or HarnessState()
        self.verifier = Verifier(timeout_sec=config.verify.timeout_sec)
        self.driver_factory = driver_factory

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
                # Each candidate runs (and is verified) in its own isolated workspace
                # rooted at the task baseline, so its captured patch and verification
                # reflect only this candidate, not the cumulative state of prior ones.
                with self._candidate_workspace(cwd) as work_cwd:
                    candidate_output = self._run_expert(expert, prompt, work_cwd, dry_run)
                    patch_text = capture_git_diff(work_cwd) or candidate_output["stdout"]
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

                    report = self.verifier.run(commands_from_config(self.config), work_cwd)
                report_doc = report.to_dict()
                report_doc["argv"] = candidate_output.get("argv")
                report_doc["agent_exit_code"] = candidate_output.get("exit_code")
                self.state.write_candidate_report(task.task_id, candidate.candidate_id, report_doc)
                scores.append(score_from_report(candidate.candidate_id, report, patch_text))
                feedback = compact_feedback(report, self.config.search.feedback_budget_chars)

        winner = select_winner(scores, selector=self.config.search.selector) if scores else None
        selection = {
            "task_id": task.task_id,
            "winner": winner.__dict__ if winner else None,
            "candidate_count": len(scores),
            "hard_pass": any(score.hard_pass for score in scores),
        }
        self.state.write_task_artifact(task.task_id, "selection.json", json.dumps(selection, indent=2))
        return selection

    def _run_expert(self, expert: ExpertSpec, prompt: str, cwd: Path, dry_run: bool) -> dict[str, Any]:
        spec = DriverSpec(
            driver=expert.driver,
            command=expert.command,
            extra_args=expert.extra_args,
        )
        driver = self.driver_factory(spec)
        argv = driver.build_argv(prompt)
        if dry_run:
            return {"argv": argv, "exit_code": 0, "stdout": "", "stderr": "", "dry_run": True}
        run = driver.run(prompt, cwd)
        return {"argv": run.argv, "exit_code": run.exit_code, "stdout": run.stdout, "stderr": run.stderr}

    @contextmanager
    def _candidate_workspace(self, cwd: Path) -> Iterator[Path]:
        """Yield a working directory for one candidate.

        When search.worktree is enabled and cwd is a git repo with a commit, the
        candidate gets a throwaway detached git worktree rooted at the current HEAD,
        so its file changes (and the captured diff) are isolated from other
        candidates and never touch the caller's working tree. Otherwise the caller's
        cwd is used directly (no isolation).
        """
        baseline = _git_head(cwd) if self.config.search.worktree else None
        if baseline is None:
            yield cwd
            return
        parent = Path(tempfile.mkdtemp(prefix="rsi-wt-"))
        work = parent / "wt"
        if not _git_worktree_add(cwd, work, baseline):
            shutil.rmtree(parent, ignore_errors=True)
            print(
                f"rsi: warning: worktree isolation was requested but `git worktree add` failed; "
                f"running this candidate in {cwd} (the working tree may be modified)",
                file=sys.stderr,
            )
            yield cwd
            return
        try:
            yield work
        finally:
            _git_worktree_remove(cwd, work)
            shutil.rmtree(parent, ignore_errors=True)


def commands_from_config(config: HarnessConfig) -> list[VerificationCommand]:
    return [
        VerificationCommand(
            name=command.name,
            run=command.run,
            timeout_sec=command.timeout_sec,
            max_runtime_sec=command.max_runtime_sec,
        )
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


def _git_head(cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_worktree_add(cwd: Path, work: Path, baseline: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "worktree", "add", "--detach", str(work), baseline],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _git_worktree_remove(cwd: Path, work: Path) -> None:
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(work)],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


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
    patch_penalty = min(len(patch_text) / 20000, 10)
    runtime_penalty = min(report.runtime_sec / 120, 10)
    return CandidateScore(
        candidate_id=candidate_id,
        hard_pass=report.hard_pass,
        test_pass_rate=_test_pass_rate(report),
        generated_test_pass_rate=0.0,
        behavioral_vote_count=1,
        static_quality_score=0.5 if _looks_like_diff(patch_text) else 0.0,
        risk_score=_diff_risk(patch_text),
        normalized_runtime=runtime_penalty,
        patch_size_penalty=patch_penalty,
        output_bucket=report.output_bucket,
    )


def _test_pass_rate(report: VerificationReport) -> float:
    """Fraction of passing tests, parsed from runner output when possible.

    Falls back to the fraction of verification *commands* that passed when no
    machine-readable test counts (unittest/pytest) can be parsed.
    """
    counts = [
        parsed
        for parsed in (_parse_test_counts((r.stdout or "") + "\n" + (r.stderr or "")) for r in report.results)
        if parsed is not None
    ]
    if counts:
        passed = sum(p for p, _ in counts)
        total = sum(t for _, t in counts) or 1
        return passed / total
    pass_count = sum(1 for result in report.results if result.exit_code == 0 and not result.timed_out)
    return pass_count / (len(report.results) or 1)


def _parse_test_counts(text: str) -> tuple[int, int] | None:
    """Return (passed, countable_total) parsed from unittest or pytest output, or None.

    Skipped tests are excluded from both passed and total (they are neutral, not
    wins). To avoid false positives, the pytest path only reads the framed summary
    line (e.g. ``==== 3 failed, 7 passed in 0.4s ====``), never arbitrary prose.
    """
    # unittest: "Ran N tests" plus an OK/FAILED detail block using name=N tokens.
    ran = re.search(r"Ran (\d+) tests?\b", text)
    if ran:
        total = int(ran.group(1))
        skipped = _named_count(text, "skipped")
        failed = _named_count(text, "failures") + _named_count(text, "errors")
        countable = total - skipped
        if countable <= 0:
            return None
        return max(countable - failed, 0), countable

    # pytest: trust ONLY the framed summary line, not narrative output elsewhere.
    summary = re.search(r"^=+.*\b\d+\s+(?:passed|failed|errors?|skipped)\b.*=+\s*$", text, re.MULTILINE)
    if not summary:
        return None
    line = summary.group(0)
    passed = _word_count(line, "passed")
    failed = _word_count(line, "failed") + _word_count(line, "error")
    countable = passed + failed
    return (passed, countable) if countable else None


def _named_count(text: str, name: str) -> int:
    """Read a unittest-style ``name=N`` count (e.g. failures=2, skipped=3)."""
    match = re.search(name + r"=(\d+)", text)
    return int(match.group(1)) if match else 0


def _word_count(text: str, word: str) -> int:
    """Read a pytest-style ``N word`` count (e.g. 7 passed, 1 error)."""
    match = re.search(r"(\d+)\s+" + word, text)
    return int(match.group(1)) if match else 0


def _diff_risk(patch_text: str) -> float:
    """Risk proxy from a unified diff: more deleted lines => higher risk, capped at 1.0."""
    deletions = sum(
        1 for line in patch_text.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    return min(deletions / 100.0, 1.0)


def _looks_like_diff(patch_text: str) -> bool:
    stripped = patch_text.lstrip()
    return "diff --git" in patch_text or stripped.startswith("diff ") or "@@" in patch_text

