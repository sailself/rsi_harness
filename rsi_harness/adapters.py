from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DriverSpec:
    driver: str
    command: str | None = None
    extra_args: list[str] = field(default_factory=list)
    timeout_sec: int = 900


@dataclass(frozen=True)
class DriverRun:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str


class AgentDriver:
    default_command = ""

    def __init__(self, spec: DriverSpec) -> None:
        self.spec = spec

    def build_argv(self, prompt: str) -> list[str]:
        command = self.spec.command or self.default_command
        argv = _split_command(command) + list(self.spec.extra_args)
        if "{prompt}" in argv:
            return [prompt if part == "{prompt}" else part for part in argv]
        argv.append(prompt)
        return argv

    def run(self, prompt: str, cwd: Path) -> DriverRun:
        argv = self.build_argv(prompt)
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.spec.timeout_sec,
            check=False,
        )
        return DriverRun(
            argv=argv,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class CodexDriver(AgentDriver):
    default_command = "codex exec --skip-git-repo-check --sandbox workspace-write"


class ClaudeDriver(AgentDriver):
    default_command = "claude -p"


class OpenCodeDriver(AgentDriver):
    default_command = "opencode run"


def build_driver(spec: DriverSpec) -> AgentDriver:
    driver = spec.driver.lower().replace("_", "-")
    if driver == "codex":
        return CodexDriver(spec)
    if driver in {"claude", "claude-code"}:
        return ClaudeDriver(spec)
    if driver == "opencode":
        return OpenCodeDriver(spec)
    raise ValueError(f"Unsupported driver: {spec.driver}")


def _split_command(command: str) -> list[str]:
    return shlex.split(command, posix=os.name != "nt")

