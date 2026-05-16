# RSI Harness

`rsi-harness` is a small, standard-library Python harness for improving coding-agent output quality through external verification loops. It is designed to work with Codex, Claude Code, and OpenCode without fine-tuning or model-internal access.

## Design Principles

The Poetiq article describes a meta-system that improves coding results by constructing and optimizing an external harness around standard LLM APIs, accounting for correctness, runtime, and memory constraints, and reusing the learned harness across models. This project implements the transferable part of that idea:

- Keep generation in the agent, but move judgment into a harness.
- Store every candidate as a diff, prompt, command log, and verification report.
- Prefer hard executable gates over model self-assessment.
- Use compact failing evidence as the next repair prompt.
- Select winners by hard pass first, then test pass rate, behavioral vote count, quality, risk, runtime, and patch size.
- Keep the same core usable from Codex hooks, Claude hooks, OpenCode prompts, batch mode, or MCP.

Reference points checked while building this scaffold:

- Poetiq RSI article: https://poetiq.ai/posts/recursive_self_improvement_coding/
- Codex hooks/plugins/skills docs: https://developers.openai.com/codex/hooks, https://developers.openai.com/codex/plugins/build, https://developers.openai.com/codex/skills
- Claude Code hooks/plugins/skills docs: https://code.claude.com/docs/en/hooks, https://code.claude.com/docs/en/plugins, https://code.claude.com/docs/en/skills
- OpenCode CLI docs: https://open-code.ai/en/docs/cli

## Quick Start

Run the local checks:

```sh
python -m unittest discover -s tests -v
python -m rsi_harness.cli verify --json
```

Create starter config in another project:

```sh
python -m rsi_harness.cli init
```

Run verification from configured `.rsi.yaml`:

```sh
rsi verify --changed --json
```

Run batch candidate search:

```sh
rsi run --task issue.md --experts experts.yaml --rounds 3
rsi select --task latest --json
```

Use `--dry-run` to validate prompts and state creation without invoking Codex, Claude, or OpenCode:

```sh
rsi run --task issue.md --rounds 1 --dry-run --json
```

## Integration Surfaces

Codex plugin:

- `.codex-plugin/plugin.json`
- `skills/rsi-coding/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

Claude Code plugin:

- `.claude-plugin/plugin.json`
- `skills/rsi-coding/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

OpenCode:

- `AGENTS.md` tells OpenCode to use the `rsi` CLI.
- `experts.yaml` includes an `opencode run` expert for batch mode.

## Core Commands

`rsi verify`

Runs configured hard gates from `.rsi.yaml`. With `--changed`, it uses `changed_file_rules` when files match.

`rsi task`

Creates `.rsi/tasks/<task_id>/task.json`.

`rsi run`

Creates candidates by invoking configured external agent CLIs, captures prompts/logs/diffs, runs verification, compacts feedback, and writes `selection.json`.

`rsi hook`

Entry point for Codex and Claude lifecycle hooks. It can create tasks from complex prompts, block known destructive commands, run affected verification after tools, and surface failing evidence at stop time.

`python -m rsi_harness.mcp_server`

Minimal stdio MCP server exposing `rsi.create_task`, `rsi.run_verify`, and `rsi.select_winner`.

