from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    run: str
    timeout_sec: int | None = None


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    runtime_sec: float
    timed_out: bool = False


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
        hard_pass = bool(commands) and all(result.exit_code == 0 and not result.timed_out for result in results)
        return VerificationReport(
            hard_pass=hard_pass,
            results=results,
            runtime_sec=runtime,
            output_bucket=_output_bucket(results),
        )

    def _run_one(self, command: VerificationCommand, cwd: Path) -> CommandResult:
        timeout = command.timeout_sec or self.timeout_sec
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
            return CommandResult(
                name=command.name,
                command=command.run,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                runtime_sec=time.monotonic() - started,
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


def _output_bucket(results: list[CommandResult]) -> str:
    digest = hashlib.sha256()
    for result in results:
        digest.update(result.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(result.exit_code).encode("utf-8"))
        digest.update(b"\0")
        digest.update(result.stdout.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(result.stderr.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

