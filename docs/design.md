# RSI Harness Design

## Mechanism

The harness implements recursive improvement as an outer loop:

```text
task spec
-> N agent candidates
-> compile/test/typecheck/lint/bench
-> compact executable failures
-> next candidate prompt
-> selector chooses the best verified patch
```

The important separation is that agents generate and repair, while the harness records and judges. This keeps the system model-agnostic: Codex, Claude Code, and OpenCode can all be plugged in as candidate producers.

## Components

- `rsi_harness.config`: loads `.rsi.yaml` without requiring PyYAML.
- `rsi_harness.verifier`: runs commands with timeout and records stdout, stderr, exit code, runtime, and output hash.
- `rsi_harness.selector`: scores candidates with hard-pass priority and soft quality/risk/runtime signals.
- `rsi_harness.state`: writes `.rsi/tasks/<task_id>/*` artifacts.
- `rsi_harness.adapters`: builds command invocations for `codex exec`, `claude -p`, and `opencode run`.
- `rsi_harness.orchestrator`: batch loop for experts, rounds, feedback, verification, and winner selection.
- `rsi_harness.hooks`: lifecycle hook logic for interactive Codex and Claude sessions.
- `rsi_harness.mcp_server`: minimal MCP bridge.

## Selection Formula

```text
score =
  1000 * hard_pass
  + 80 * test_pass_rate
  + 40 * generated_test_pass_rate
  + 30 * behavioral_vote_count
  + 20 * static_quality_score
  - 30 * risk_score
  - 10 * normalized_runtime
  - 5  * patch_size_penalty
```

Hard gates dominate because a failing candidate should not beat a passing one on style or confidence.

## Limits

This is an installable scaffold, not Poetiq's proprietary auto-learned meta-system. It does not yet learn new verifier strategies over many historical tasks. The extension point for that is the persisted `.rsi/tasks` corpus plus selector/report data.

