# RSI Harness Instructions

This repo provides a recursive self-improvement harness for coding agents. Codex and Claude Code can load the bundled skill and hooks through their plugin systems. OpenCode should treat this file as the project instruction surface and use the same `rsi` CLI directly.

For complex coding tasks:

1. Write a measurable task spec before editing.
2. Prefer tests or checks that can fail before implementation.
3. Run `rsi verify --changed --json` after material edits.
4. Use `rsi run --task <file-or-text> --experts experts.yaml --rounds <N>` for difficult bugs, algorithms, risky refactors, or performance work.
5. Report exact commands, candidate selection evidence, and residual risk.

OpenCode non-interactive example:

```sh
opencode run "Use the RSI harness: inspect the task, implement the fix, run rsi verify --changed --json, and summarize evidence."
```

