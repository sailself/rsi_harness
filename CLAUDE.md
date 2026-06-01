# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`rsi-harness` is a model-agnostic **external verification loop** for CLI coding agents. It does not fine-tune or inspect model internals. The whole design rests on one separation of concerns: **external agents generate and repair code; this harness records and judges the result by executable evidence.** Understanding that split explains almost every module — anything that runs a model lives behind a thin adapter, and everything else is deterministic plumbing for capturing, verifying, scoring, and persisting candidates.

Pure stdlib, no required dependencies. Python ≥ 3.10.

## Commands

```sh
# Run the full test suite (stdlib unittest; there is no pytest config)
python -m unittest discover -s tests -v

# Run ONE test — the obvious dotted form FAILS here (see gotcha below).
# Use discover with -k to filter. -k matches a method OR class name substring.
python -m unittest discover -s tests -p test_rsi_harness.py -k <method_or_class_name> -v

# Run the harness's own verifier against this repo (driven by ./.rsi.yaml -> runs the test suite)
python -m rsi_harness.cli verify --json

# Run the CLI without installing (module entry point; what bin/rsi.cmd uses on Windows)
python -m rsi_harness.cli <subcommand> ...

# Install the `rsi` console script (pyproject [project.scripts] rsi = rsi_harness.cli:main)
pip install -e .

# Launch the MCP stdio server
python -m rsi_harness.mcp_server
```

There is **no separate lint/typecheck/format step** — the test suite (`tests/test_rsi_harness.py`) is the only gate, and `rsi verify` in this repo just runs it.

PyYAML is optional: `config.py` falls back to an in-repo minimal YAML parser when it is absent. The external agent CLIs (`codex`, `claude`, `opencode`) are only needed for *real* `rsi run` candidate generation — use `--dry-run` to exercise the loop without them.

## Architecture

The package is three layers around a deterministic core.

**1. Judging core (deterministic, model-free):**
- `config.py` — loads `.rsi.yaml`/`.json` into frozen dataclasses (`HarnessConfig` → `VerifyConfig` + `SearchConfig`). `_load_structured_data` prefers PyYAML but silently falls back to `_parse_minimal_yaml` on *any* parse exception.
- `verifier.py` — `Verifier.run(commands, cwd)` executes each `VerificationCommand` via `subprocess.run(shell=True)`, producing a `VerificationReport` with `hard_pass`, per-command results, and a timing-independent `output_bucket` (SHA-256 over name/exit/stdout/stderr — the intended behavioral-clustering key).
- `selector.py` — `select_winner(candidates)` scores each `CandidateScore` with a fixed linear formula and tie-break sorts.
- `state.py` — `HarnessState` owns the on-disk `.rsi/` artifact tree and the timestamp+hash ID scheme.

**2. Candidate search (the only model-facing path):**
- `adapters.py` — `build_driver(name)` maps a driver string to `CodexDriver` / `ClaudeDriver` (alias `claude-code`) / `OpenCodeDriver`. Each is a thin `AgentDriver` whose only real difference is its `default_command`. `build_argv` substitutes a literal `{prompt}` token if present, otherwise **appends** the prompt as the trailing arg.
- `orchestrator.py` — `Orchestrator.run(...)` is the recursive-improvement outer loop. For each `round × expert`: build a prompt → invoke the agent subprocess → capture the working-tree diff as the patch → verify → score → compact failing output into feedback for the next iteration → finally `select_winner` and write `selection.json`.

**3. Integration / entry points:**
- `cli.py` — single argparse entry point, six subcommands (`init`, `verify`, `task`, `run`, `select`, `hook`), each a thin `cmd_*` handler. `main()` returns the process exit code.
- `hooks.py` (+ `bin/rsi_hook.py`) — lifecycle hooks for interactive Codex/Claude sessions (`session-start`, `prompt-submit`, `pre-tool`, `post-tool`, `stop`).
- `mcp_server.py` — hand-rolled JSON-RPC 2.0 stdio server exposing three tools.

### The `rsi run` loop (read `orchestrator.py:53` to follow this)

```
task spec
 └─ for round in 1..rounds:
     └─ for expert in experts:
         build_candidate_prompt(spec, prompt_variant, feedback)   # feedback = prior candidate's failures
          → driver.run(prompt, cwd)                                # external agent CLI subprocess
          → capture_git_diff(cwd) or agent stdout                  # the candidate patch
          → state.create_candidate(...) + write prompt.md/agent.json/report.json
          → verifier.run(commands_from_config(config), cwd)        # always the FULL command set
          → score_from_report(...)
          → feedback = compact_feedback(report, budget_chars)      # rolling, shared, overwritten each iter
 └─ select_winner(scores) → selection.json
```

### Artifacts (`state.py` writes these)

```
.rsi/
  tasks/<task_id>/
    task.json                 # spec + metadata
    selection.json            # winner + candidate_count
    candidates/<candidate_id>/
      prompt.md  candidate.patch  candidate.json  agent.json  report.json
  hook-feedback/latest.json   # last hook verification report (post-tool writes, stop reads)
```
IDs are `<prefix>-<UTC %Y%m%d%H%M%S%f>-<8hex>`, so lexical sort == chronological (`latest_task_id` relies on this).

### Selection formula (`selector.py:20`)

```
score = 1000*hard_pass + 80*test_pass_rate + 40*generated_test_pass_rate
      + 30*behavioral_vote_count + 20*static_quality_score
      - 30*risk_score - 10*normalized_runtime - 5*patch_size_penalty
```
Hard pass dominates by design — a failing candidate must never beat a passing one on style. Tie-break: score, then `hard_pass`, then largest (newest) `candidate_id`.

### How the three agents consume this repo

The plugin manifests (`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`), `hooks/hooks.json`, `.mcp.json`, `skills/rsi-coding/SKILL.md`, `experts.yaml`, and `.rsi.yaml` are **shared**. Only the two manifests and `AGENTS.md` differ per agent. Codex declares `skills=./skills` explicitly; Claude Code auto-discovers `skills/`. OpenCode has no plugin loader and reads `AGENTS.md` as its instruction surface, driving the `rsi` CLI directly. `experts.yaml` (id/driver/command/`prompt_variant`) defines the candidate panel.

## Non-obvious behaviors (these will bite you)

- **Single-test invocation.** `python -m unittest tests.test_rsi_harness.<Class>.<method>` **fails** — there is no `tests/__init__.py`, so `tests` is not an importable package, and running the module from the `tests/` dir loses `rsi_harness` from `sys.path`. The only form that satisfies both constraints is `discover -s tests -p test_rsi_harness.py -k <name>`.
- **`rsi run` exit code reflects verification.** It returns `0` only when the selected winner hard-passed, else `1`; the selection dict carries a top-level `hard_pass`, and an empty candidate set yields `winner: null` instead of crashing. (`rsi verify` exits 1 on hard-fail; `rsi` with no subcommand exits 2.)
- **Candidate isolation.** When `search.worktree: true` (the default written by `rsi init`) and `cwd` is a git repo with a commit, each candidate runs and is verified in a throwaway detached `git worktree` rooted at HEAD, so its captured `.patch` and verification reflect only that candidate and the caller's working tree is never touched (`orchestrator.py` `_candidate_workspace`). With `worktree: false` (or no git), candidates share `cwd` and their diffs accumulate — use a clean branch there.
- **`prompt_variant` is advisory text only.** `build_candidate_prompt` injects it as a literal `Prompt variant: <x>` line; there is no code branching on `direct` / `tests-first` / `adversarial-review`. Arbitrary strings are accepted.
- **`changed_file_rules` does NOT affect `rsi run`.** The run loop always uses the full `verify.commands` (`commands_from_config`). The changed-file rules only apply to `rsi verify --changed` and the `post-tool` hook (`changed_commands_from_config`, which falls back to the full set when no glob matches).
- **Scoring signals are mostly real now.** `test_pass_rate` is parsed from unittest/pytest output (falling back to the command-pass fraction); `risk_score` comes from diff deletions; `static_quality_score` rewards a real unified diff over agent chatter. **Behavioral voting** is implemented but opt-in: set `search.selector: behavioral_vote` and `select_winner` clusters candidates by `output_bucket` into normalized vote shares (default `selector: score` leaves vote at 1). Ties break on merit (test pass rate, votes, smaller/faster patch) before `candidate_id`. Still stubbed: `generated_test_pass_rate` (0.0).
- **Runtime can be a hard gate.** A `verify.commands` entry may set `max_runtime_sec`; a command that finishes but exceeds it is `runtime_exceeded` and excluded from `hard_pass` (distinct from the kill-`timeout_sec`). Memory is still **not** enforced (`memory_mb` is metadata only).
- **Agent subprocess timeout is hardcoded to 900s** (`config.verify.timeout_sec` applies only to *verification* commands). A timed-out or missing agent CLI is now caught and recorded as a failed candidate (`adapters.py`) rather than crashing the whole run.
- **`rsi init` writes the Python config** (command `unit` → `python -m unittest discover -s tests`, `selector: score`, `worktree: true`) matching this repo and the README — safe to run here. It is atomic: it refuses to write anything if either target exists without `--force`.
- **Malformed config fails loudly.** When PyYAML is installed, an invalid `.rsi.yaml`, a `verify.commands` entry missing `name`/`run`, or a non-integer numeric field raises `ConfigError` (no silent fallback to the minimal parser). The minimal parser is only used when PyYAML is absent.
- **`--json` error envelope.** Success shapes still differ per subcommand (`verify`→`VerificationReport`, `task`→`TaskRecord`, `run`→selection dict, `select`→raw `selection.json`), but any error now prints `{"ok": false, "error": {...}}` to stdout under `--json` (plain stderr otherwise) instead of a traceback.
- **Hook event labels are correct per event** (`SessionStart`/`UserPromptSubmit`/`PostToolUse` via `hookSpecificOutput`; `Stop` via top-level `systemMessage`).
- **MCP tools** are named `rsi_create_task` / `rsi_run_verify` / `rsi_select_winner` (underscores, matching the surfaced `mcp__rsi-harness__*` form). `.mcp.json` registers the server under both `mcp_servers` and `mcpServers` keys (a Codex/Claude compatibility hedge) — keep them in sync. `rsi_select_winner` validates `task_id` (rejects path traversal); a malformed stdin line yields a `-32700` parse error instead of crashing the loop.
- **The destructive-command hook is advisory defense-in-depth, not a security boundary.** `DESTRUCTIVE_PATTERNS` is a small denylist and `_pre_tool` only emits a `deny` decision (it returns exit 0); real isolation comes from per-candidate worktrees, not this regex. The `PreToolUse`/`PostToolUse` matchers cover `Bash|Edit|Write|MultiEdit|apply_patch`.

## Working in this repo

Per `AGENTS.md` / `skills/rsi-coding/SKILL.md`, the intended workflow for non-trivial tasks: write a measurable spec first, prefer checks that can fail before implementation, run `rsi verify --changed --json` after material edits, reach for `rsi run --task <file-or-text> --experts experts.yaml --rounds <N>` on hard bugs/refactors/perf work, and report exact commands plus selection evidence.

This repo is an installable scaffold, not a learned meta-system: it does not yet learn verifier strategies from history, and `verify.memory_mb` is config metadata only (timeouts are enforced; OS memory limits are not).
