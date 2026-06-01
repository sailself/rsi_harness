"""Read-only analytics over the persisted ``.rsi/tasks`` corpus.

This is the first practical step of cross-task recursion: the harness writes a
rich corpus of tasks/candidates/reports/selections, and this module reads it back
so prior outcomes can inform the next run (expert ordering, prompt priors). It is
deliberately read-only and deterministic — not the article's full auto-optimizing
meta-system, but the seam that turns the write-only corpus into a feedback source.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import HarnessState


@dataclass(frozen=True)
class CorpusStats:
    task_count: int = 0
    candidate_count: int = 0
    expert_win_counts: dict[str, int] = field(default_factory=dict)
    expert_hard_pass_counts: dict[str, int] = field(default_factory=dict)
    expert_total_counts: dict[str, int] = field(default_factory=dict)
    failing_commands: dict[str, int] = field(default_factory=dict)

    def expert_win_rate(self, expert_id: str) -> float:
        total = self.expert_total_counts.get(expert_id, 0)
        return self.expert_win_counts.get(expert_id, 0) / total if total else 0.0

    def top_failing_commands(self, limit: int = 5) -> list[tuple[str, int]]:
        return sorted(self.failing_commands.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_count": self.task_count,
            "candidate_count": self.candidate_count,
            "expert_win_counts": self.expert_win_counts,
            "expert_hard_pass_counts": self.expert_hard_pass_counts,
            "expert_total_counts": self.expert_total_counts,
            "failing_commands": self.failing_commands,
        }


def load_corpus_stats(state: HarnessState) -> CorpusStats:
    tasks_root = state.tasks_root
    if not tasks_root.exists():
        return CorpusStats()

    task_count = 0
    candidate_count = 0
    wins: dict[str, int] = {}
    hard_passes: dict[str, int] = {}
    totals: dict[str, int] = {}
    failing: dict[str, int] = {}

    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        task_count += 1
        id_to_expert: dict[str, str] = {}
        candidates_dir = task_dir / "candidates"
        if candidates_dir.exists():
            for cand_dir in candidates_dir.iterdir():
                if not cand_dir.is_dir():
                    continue
                meta = _read_json(cand_dir / "candidate.json")
                if not meta:
                    continue
                expert_id = str(meta.get("expert_id", ""))
                if not expert_id:
                    continue  # malformed candidate; don't create a phantom "" expert
                candidate_id = str(meta.get("candidate_id", cand_dir.name))
                id_to_expert[candidate_id] = expert_id
                candidate_count += 1
                totals[expert_id] = totals.get(expert_id, 0) + 1

                report = _read_json(cand_dir / "report.json")
                if report:
                    if report.get("hard_pass"):
                        hard_passes[expert_id] = hard_passes.get(expert_id, 0) + 1
                    for result in report.get("results", []):
                        # Mirror the verifier's hard_pass exclusions: a command fails
                        # if it errored, timed out, or exceeded its runtime budget.
                        if result.get("exit_code", 0) != 0 or result.get("timed_out") or result.get("runtime_exceeded"):
                            name = str(result.get("name", ""))
                            failing[name] = failing.get(name, 0) + 1

        selection = _read_json(task_dir / "selection.json")
        winner = (selection or {}).get("winner")
        if winner:
            winner_expert = id_to_expert.get(str(winner.get("candidate_id", "")))
            if winner_expert:
                wins[winner_expert] = wins.get(winner_expert, 0) + 1

    return CorpusStats(
        task_count=task_count,
        candidate_count=candidate_count,
        expert_win_counts=wins,
        expert_hard_pass_counts=hard_passes,
        expert_total_counts=totals,
        failing_commands=failing,
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None
