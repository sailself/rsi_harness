from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    spec: str
    metadata: dict[str, Any]
    created_at: str
    path: str


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    task_id: str
    expert_id: str
    round_index: int
    driver: str
    patch_path: str
    metadata_path: str


class HarnessState:
    def __init__(self, root: Path = Path(".rsi")) -> None:
        self.root = root
        self.tasks_root = root / "tasks"

    def create_task(self, spec: str, metadata: dict[str, Any] | None = None) -> TaskRecord:
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        task_id = _new_id("T", spec)
        path = self.task_dir(task_id)
        path.mkdir(parents=True, exist_ok=False)
        record = TaskRecord(
            task_id=task_id,
            spec=spec,
            metadata=metadata or {},
            created_at=_now(),
            path=str(path),
        )
        _write_json(path / "task.json", asdict(record))
        (path / "candidates").mkdir(exist_ok=True)
        (path / "feedback").mkdir(exist_ok=True)
        return record

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_root / task_id

    def latest_task_id(self) -> str:
        if not self.tasks_root.exists():
            raise FileNotFoundError("No .rsi/tasks directory exists")
        tasks = sorted((path for path in self.tasks_root.iterdir() if path.is_dir()), key=lambda p: p.name)
        if not tasks:
            raise FileNotFoundError("No RSI tasks have been created")
        return tasks[-1].name

    def create_candidate(
        self,
        task_id: str,
        expert_id: str,
        round_index: int,
        driver: str,
        patch_text: str,
    ) -> CandidateRecord:
        candidate_id = _new_id(f"{expert_id}-r{round_index}", patch_text)
        candidate_dir = self.task_dir(task_id) / "candidates" / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=False)
        patch_path = candidate_dir / "candidate.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        metadata_path = candidate_dir / "candidate.json"
        record = CandidateRecord(
            candidate_id=candidate_id,
            task_id=task_id,
            expert_id=expert_id,
            round_index=round_index,
            driver=driver,
            patch_path=str(patch_path),
            metadata_path=str(metadata_path),
        )
        _write_json(metadata_path, asdict(record))
        return record

    def write_candidate_report(self, task_id: str, candidate_id: str, report: dict[str, Any]) -> Path:
        path = self.task_dir(task_id) / "candidates" / candidate_id / "report.json"
        _write_json(path, report)
        return path

    def write_task_artifact(self, task_id: str, name: str, content: str) -> Path:
        path = self.task_dir(task_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _new_id(prefix: str, seed: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    digest = hashlib.sha256((timestamp + seed).encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{timestamp}-{digest}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

