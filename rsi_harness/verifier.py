from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    run: str
    timeout_sec: int | None = None
    max_runtime_sec: int | None = None


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    runtime_sec: float
    timed_out: bool = False
    runtime_exceeded: bool = False


@dataclass(frozen=True)
class VerificationReport:
    hard_pass: bool
    results: list[CommandResult]
    runtime_sec: float
    output_bucket: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class Verifier:
    def __init__(self, timeout_sec: int = 180) -> None:
        self.timeout_sec = timeout_sec

    def run(self, commands: list[VerificationCommand], cwd: Path) -> VerificationReport:
        started = time.monotonic()
        results = [self._run_one(command, cwd) for command in commands]
        runtime = time.monotonic() - started
        hard_pass = bool(commands) and all(
            result.exit_code == 0 and not result.timed_out and not result.runtime_exceeded for result in results
        )
        return VerificationReport(
            hard_pass=hard_pass,
            results=results,
            runtime_sec=runtime,
            output_bucket=_output_bucket(results),
        )

    def _run_one(self, command: VerificationCommand, cwd: Path) -> CommandResult:
        timeout = self.timeout_sec if command.timeout_sec is None else command.timeout_sec
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command.run,
                cwd=str(cwd),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            runtime = time.monotonic() - started
            exceeded = command.max_runtime_sec is not None and runtime > command.max_runtime_sec
            return CommandResult(
                name=command.name,
                command=command.run,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                runtime_sec=runtime,
                runtime_exceeded=exceeded,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                name=command.name,
                command=command.run,
                exit_code=-1,
                stdout=_decode_timeout_output(exc.stdout),
                stderr=_decode_timeout_output(exc.stderr) + f"\nTimed out after {timeout}s",
                runtime_sec=time.monotonic() - started,
                timed_out=True,
            )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DURATION_RE = re.compile(r"\b\d+\.\d+s\b")


def _normalize_output(text: str) -> str:
    """Strip volatile, behaviorally-irrelevant tokens so identical runs share a bucket.

    Removes ANSI color codes and masks fractional-second durations (e.g. unittest's
    "Ran 3 tests in 0.12s" / pytest's "in 1.2s"), which vary run to run.
    """
    text = _ANSI_RE.sub("", text)
    text = _DURATION_RE.sub("<dur>", text)
    return text


def _output_bucket(results: list[CommandResult]) -> str:
    digest = hashlib.sha256()
    for result in results:
        digest.update(result.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(result.exit_code).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalize_output(result.stdout).encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(_normalize_output(result.stderr).encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

