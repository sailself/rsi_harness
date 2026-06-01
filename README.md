# RSI Harness

`rsi-harness` is a small Python harness for improving coding-agent output with an
external verification loop. It does not fine-tune a model or inspect model
internals. Instead, it asks one or more CLI coding agents to produce candidate
patches, runs configured checks, records the evidence, and selects the best
candidate by executable results.

The project is designed to be usable from:

- Codex, through the bundled plugin manifest, skill, hooks, and MCP server.
- Claude Code, through the bundled plugin manifest, skill, hooks, and MCP server.
- OpenCode, through `AGENTS.md` instructions and direct `rsi` CLI usage.
- Plain shell scripts or CI jobs, through `python -m rsi_harness.cli` or the
  installed `rsi` command.

## What It Does

The harness wraps the parts of recursive self-improvement that are practical for
normal coding-agent workflows:

1. Capture the task as a measurable spec.
2. Generate one or more candidate patches with external agent CLIs.
3. Run hard verification commands such as tests, lint, typecheck, or benchmarks.
4. Compact failing command output into feedback for the next round.
5. Persist prompts, patches, agent logs, verification reports, and selection data.
6. Pick a winner by hard pass first, then test pass rate, vote count, quality,
   risk, runtime, and patch size.

The important separation is that agents generate code, while the harness records
and judges the result. That keeps the workflow model-agnostic and makes the
evidence inspectable after the run.

## When To Use It

Use the lightweight verifier for ordinary implementation work:

```sh
rsi verify --changed --json
```

Use the full candidate loop when a task benefits from multiple attempts or
repair rounds:

```sh
rsi run --task issue.md --experts experts.yaml --rounds 3
rsi select --task latest --json
```

Good fits include difficult bugs, risky refactors, algorithm changes,
performance work, behavior that needs regression tests, and cases where you want
several agents or prompt variants to compete against the same checks.

## Requirements

- Python 3.10 or newer.
- No required Python package dependencies.
- Optional external agent CLIs for candidate search:
  - `codex`
  - `claude`
  - `opencode`
- A Git worktree when you want changed-file verification or patch capture.

You can run commands from the repository checkout with:

```sh
python -m rsi_harness.cli --help
```

After installing the package, the same CLI is available as `rsi`:

```sh
pip install -e .
rsi --help
```

## Quick Start

Run the project checks in this repository:

```sh
python -m unittest discover -s tests -v
python -m rsi_harness.cli verify --json
```

Create starter harness files in another project:

```sh
rsi init
```

That writes:

- `.rsi.yaml`, which defines verification and search behavior.
- `experts.yaml`, which defines the agent CLIs used for candidate generation.

Run verification from `.rsi.yaml`:

```sh
rsi verify --json
```

Run affected verification based on changed files:

```sh
rsi verify --changed --json
```

Create a task record without running agents:

```sh
rsi task "Fix the parser so invalid input reports the source span" --json
```

Run a dry candidate search to validate prompts and state writes without invoking
Codex, Claude Code, or OpenCode:

```sh
rsi run --task issue.md --rounds 1 --dry-run --json
```

Run a real candidate search:

```sh
rsi run --task issue.md --experts experts.yaml --rounds 3 --json
rsi select --task latest --json
```

## Configuration

`rsi init` creates a starter `.rsi.yaml` similar to this:

```yaml
verify:
  timeout_sec: 180
  memory_mb: 4096
  commands:
    - name: unit
      run: python -m unittest discover -s tests
  changed_file_rules:
    "rsi_harness/**/*.py": ["python -m unittest discover -s tests"]
    "tests/**/*.py": ["python -m unittest discover -s tests"]

search:
  experts: 3
  rounds: 2
  worktree: true
  selector: score
  feedback_budget_chars: 12000
  experts_file: experts.yaml
```

Important fields:

- `verify.timeout_sec`: default timeout for each verification command.
- `verify.commands`: full verification commands used by `rsi verify` and
  candidate scoring.
- `verify.changed_file_rules`: file-pattern-specific commands used by
  `rsi verify --changed`; if no changed rule matches, the harness falls back to
  `verify.commands`.
- `search.experts`: fallback number of Codex experts if `experts.yaml` is
  missing.
- `search.rounds`: default number of candidate-generation rounds.
- `search.feedback_budget_chars`: maximum feedback text carried into the next
  candidate prompt.
- `search.experts_file`: default experts file for `rsi run`.

The config loader accepts YAML when PyYAML is installed and otherwise falls back
to the minimal YAML subset used by the starter config.

## Experts

`experts.yaml` tells the orchestrator which external CLIs can produce
candidates:

```yaml
experts:
  - id: codex-fast-0
    driver: codex
    command: codex exec --skip-git-repo-check --sandbox workspace-write
    prompt_variant: direct
  - id: claude-deep-0
    driver: claude
    command: claude -p
    prompt_variant: tests-first
  - id: opencode-review-0
    driver: opencode
    command: opencode run
    prompt_variant: adversarial-review
```

Supported drivers are `codex`, `claude` or `claude-code`, and `opencode`.
The command is split into argv form, the generated prompt is appended, and the
agent runs in the current working directory. If a command contains a `{prompt}`
argument, that argument is replaced instead of appending the prompt.

## Core Commands

### `rsi init`

Writes starter `.rsi.yaml` and `experts.yaml` files. Existing files are
preserved unless you pass `--force`.

```sh
rsi init
rsi init --force
```

### `rsi verify`

Runs configured hard gates and exits non-zero if any command fails or times out.
Use `--json` when another tool should consume the report.

```sh
rsi verify --json
rsi verify --changed --json
rsi verify --config path/to/.rsi.yaml
```

### `rsi task`

Creates `.rsi/tasks/<task_id>/task.json` from a text spec.

```sh
rsi task "Refactor the retry loop without changing public behavior" --json
```

### `rsi run`

Runs batch candidate search. The `--task` value can be a path to a Markdown
file or literal task text.

```sh
rsi run --task issue.md --experts experts.yaml --rounds 3 --json
rsi run --task "Fix flaky timeout test" --dry-run --json
```

For each expert and round, the orchestrator builds a candidate prompt, invokes
the driver, captures the Git diff as the candidate patch when available, runs
verification, writes a report, and carries compact failure output into the next
prompt.

When `search.worktree` is true (the default) and the project is a Git repository
with at least one commit, each candidate runs and is verified inside a throwaway
detached Git worktree rooted at the current `HEAD`. This keeps every candidate's
captured patch and verification independent of the others and leaves your real
working tree untouched. Because each worktree starts from the committed `HEAD`,
candidates do not see uncommitted or untracked changes, so commit work you want
them to build on first. With `worktree: false` (or outside Git), candidates share
the current directory and their diffs accumulate, so run on a clean branch.

`rsi run` exits non-zero unless the selected winner passed hard verification, and
the JSON selection carries a top-level `hard_pass` flag plus `winner` (which is
`null` when no candidates were produced), so automation can gate on the result.

### `rsi select`

Reads `selection.json` for a task and prints the selected candidate.

```sh
rsi select --task latest --json
rsi select --task T-20260516000000000000-12345678
```

### `rsi hook`

Runs lifecycle hook behavior used by Codex and Claude Code integrations.

```sh
rsi hook session-start
rsi hook prompt-submit
rsi hook pre-tool
rsi hook post-tool
rsi hook stop
```

Hooks can add session context, create task records for complex prompts, block
known destructive commands, run changed-file verification after edits, and
surface the latest failing evidence before the session stops.

## Artifacts

The harness writes inspectable state under `.rsi/`:

```text
.rsi/
  tasks/
    <task_id>/
      task.json
      selection.json
      candidates/
        <candidate_id>/
          prompt.md
          candidate.patch
          candidate.json
          agent.json
          report.json
      feedback/
  hook-feedback/
    latest.json
```

Useful files:

- `task.json`: original task spec and metadata.
- `prompt.md`: exact prompt sent to an expert.
- `candidate.patch`: captured patch text or agent stdout when no Git diff is
  available.
- `agent.json`: argv, stdout, stderr, exit code, and dry-run flag when relevant.
- `report.json`: command results, stdout, stderr, runtime, timeout status, and
  output hash.
- `selection.json`: selected winner and candidate count.
- `hook-feedback/latest.json`: latest verification report from hook execution.

## Integrations

Codex plugin files:

- `.codex-plugin/plugin.json`
- `skills/rsi-coding/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

Claude Code plugin files:

- `.claude-plugin/plugin.json`
- `skills/rsi-coding/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

OpenCode uses the project instruction surface:

- `AGENTS.md` tells OpenCode to use the `rsi` CLI directly.
- `experts.yaml` includes an `opencode run` expert for batch mode.

The MCP server can be launched with:

```sh
python -m rsi_harness.mcp_server
```

It exposes a minimal stdio bridge for creating tasks, running verification, and
selecting winners.

## Selection

Candidates are scored with hard verification as the dominant signal:

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

A failing candidate should not beat a passing candidate on style or confidence.
The current implementation derives these signals from verification results and
patch size; richer quality and behavioral voting signals can be added on top of
the persisted artifact corpus.

## Development

Run local tests:

```sh
python -m unittest discover -s tests -v
```

Run the harness verifier:

```sh
python -m rsi_harness.cli verify --json
```

Run the changed-file verifier:

```sh
rsi verify --changed --json
```

## Current Limits

This repository is an installable scaffold, not Poetiq's proprietary learned
meta-system. It does not yet learn new verifier strategies from historical task
data. Candidate search isolates each candidate in its own Git worktree when
`search.worktree` is enabled; with isolation disabled (or outside a Git
repository) candidates run in the current worktree, so use a clean branch or
inspect the captured patches carefully when running real external agents.

`verify.memory_mb` is currently configuration metadata; verification enforces
command timeouts but does not impose an operating-system memory limit.

## References

- Poetiq RSI article: https://poetiq.ai/posts/recursive_self_improvement_coding/
- Codex hooks, plugins, and skills documentation:
  https://developers.openai.com/codex/hooks,
  https://developers.openai.com/codex/plugins/build,
  https://developers.openai.com/codex/skills
- Claude Code hooks, plugins, and skills documentation:
  https://code.claude.com/docs/en/hooks,
  https://code.claude.com/docs/en/plugins,
  https://code.claude.com/docs/en/skills
- OpenCode CLI documentation: https://open-code.ai/en/docs/cli
