# RSI-Harness — Evaluation Against the Poetiq RSI Article

*A multi-perspective code evaluation grounded in the article that inspired the project
(<https://poetiq.ai/posts/recursive_self_improvement_coding/>). Eight perspectives were
evaluated and every finding was adversarially re-verified against the source before inclusion.*

---

## 1. What the article actually proposes — and what this repo is

The Poetiq post is a **claims-and-results** piece, not a spec. Its load-bearing ideas:

- A **"Meta-System" that automatically builds and *optimizes its own* task-specific coding harnesses** — no fine-tuning, no access to model internals, standard API only.
- **"Recursive self-improvement"** = the system improves its *harness-creation process*, "constantly incorporat[ing] its learnings from previous and current tasks and datasets" across domains (ARC-AGI, HLE, coding).
- Selection is by **executable results**: on LiveCodeBench Pro a solution must be **correct AND satisfy hard runtime + memory constraints**.
- It is **model-agnostic**: the *same* optimized harness transfers and improves other models (e.g. Gemini 3.1 Pro 78.6→90.9%).
- The **mechanism is deliberately undisclosed** — no pseudocode, no scoring/voting formula, no representation of "the harness."

`rsi-harness` is explicitly an **inspired scaffold**, not the meta-system. By its own docs it implements the **outer loop** — `task → N candidates → compile/test/lint/bench → compact failures → next prompt → select best verified patch` — and openly states it does **not** learn from history yet.

> **The central evaluative axis:** the article's defining property is *recursion* (the harness improving itself and learning across tasks). This repo implements only the **outer per-task verify-and-select loop**. Judged as that scaffold it is a thoughtful, honestly-scoped foundation; judged as an implementation of the article it is **roughly one-third realized**, and one critical bug currently corrupts even the part it does implement.

---

## 2. Method & confidence

| | |
|---|---|
| Perspectives | rsi-fidelity, verification-engine, selection-scoring, orchestration-search, robustness-correctness, security-safety, integration-dx, testing-maintainability |
| Agents | 16 (8 evaluators → 8 adversarial verifiers + completeness critics) |
| Discipline | Every finding cites `file:line`; each was re-read by a skeptic instructed to **refute** it; severities were recalibrated; each dimension also got a "what did the evaluator miss" pass |

**Calibration honesty (what the verifiers changed):**
- **Downgraded:** `memory_mb`/runtime *hard gates* high→medium (they are documented scope limits, not hidden defects).
- **Refuted/corrected:** HOOK-2's claim that the Codex manifest sets `PLUGIN_ROOT` is **false** (neither manifest sets it; the `rsi_hook.py` `__file__` fallback covers it) — only the bare-`python` portability nit survives, severity→low. One TEST recommendation ("hard_pass=True ⇒ score 1000") is **wrong** because `behavioral_vote_count` defaults to 1 (→ 1030).
- **New (missed by evaluators):** an **unauthenticated MCP path-traversal** read via unsanitized `task_id` (medium).

---

## 3. Overall verdict

| Dimension | Grade | One-line |
|---|:---:|---|
| RSI fidelity | **B** | Honest, well-scoped outer loop; the *recursion* (cross-task learning, self-optimization) is entirely absent. |
| Verification engine | **C** | Correctness leg is careful; runtime/memory gates absent; no per-candidate isolation; `shell=True`. |
| Selection / scoring | **C** | Hard-pass dominance is sound, but 4–5 of 8 signals are stubs; **behavioral voting is vaporware**. |
| Orchestration / search | **D** | **Critical:** candidate patches accumulate (no isolation); sequential; feedback contaminated; uncaught timeouts. |
| Robustness / correctness | **C** | Clean dataclasses, but fragile at config load, agent invocation, and failure signaling. |
| Security / safety | **C** | Acceptable for solo local use but adds real sharp edges (`shell=True` over repo config, no isolation, weak denylist). |
| Integration / DX | **C** | Broad surface, but `rsi init` misconfigures, `--json` non-uniform, hook event mislabeled. |
| Testing / maintainability | **D** | The "judge" (orchestrator/hooks/mcp) is untested; no CI/mypy/lint; doesn't dogfood its own thesis. |

**Bottom line.** The *architecture* is genuinely good — a clean generate/judge split, frozen dataclasses, a persisted artifact corpus, and an `output_bucket` clustering primitive that is exactly what real behavioral voting needs. The problem is that the **load-bearing mechanisms are stubbed or unsafe**: candidate isolation (critical), selection richness, and the recursion itself. The docs are unusually honest about the scope, which keeps it accurate rather than misleading — but several "knobs" (`selector: behavioral_vote`, `worktree`) advertise capabilities that **do nothing**.

---

## 4. Verified strengths

- **Clean generate/judge separation.** Models live behind thin `AgentDriver`s; everything else is deterministic plumbing. A meta-learner could be added without disturbing the judging core. `orchestrator.py:53-91`, `adapters.py:71-79`
- **Hard-pass dominance is correct.** `1000 × hard_pass` mathematically guarantees a failing candidate can't beat a passing one on style; soft terms are bounded. `selector.py:21-31`
- **The behavioral-clustering primitive already exists.** `output_bucket` is a timing-independent SHA-256 over command name/exit/stdout/stderr, persisted in every report — the exact key real voting needs. `verifier.py:93-104`
- **Executable feedback is real.** `compact_feedback` extracts only failing output, budget-bounded, into the next prompt. `orchestrator.py:177-185`
- **Timeout handling in the verifier is lossless** (partial output captured, decoded, marked). `verifier.py:81-90`
- **Graceful git/MCP boundaries** (argv form, bounded timeouts, structured JSON-RPC errors). `orchestrator.py:129-160`, `mcp_server.py:54-69`
- **Honest, specific limitation docs** in README/design.md/CLAUDE.md.

---

## 5. Findings by theme (all confirmed unless noted)

### Theme A — The recursion gap (article-fidelity core)
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| RSI-1 | High | **No meta-learning.** Nothing ever reads the `.rsi/tasks` corpus back; the only traversal is `latest_task_id` (lexical sort). No prompt/selector/expert/verifier is influenced by history. The META loop is *absent, not stubbed*. | `state.py:56-62`, `orchestrator.py:61-91` |
| RSI-5 | Low | **No self-optimization seam.** Prompts are a fixed template; verifier commands are verbatim config. No hook where a meta-layer could substitute strategy. | `orchestrator.py:163-174,107-111` |
| RSI-2 | Med | **Name overclaims.** Package/docstring assert "recursive self-improvement"; honest disclaimers are trailing. Inner feedback is *rolling* (overwritten), not *accumulating*. | `__init__.py:1`, `orchestrator.py:86` |

### Theme B — Candidate isolation (the linchpin defect)
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| ORCH-1 | **Critical** | **Patch accumulation.** The working tree is never reset/isolated between candidates, so candidate *N*'s captured diff = candidates 1..*N*. This corrupts every multi-candidate run: wrong `candidate.patch`, monotonically-growing `patch_size_penalty`, and **non-independent verification** (a later candidate "passes" because an earlier one fixed the build). | `orchestrator.py:64-87,68,147-160` |
| ISO-1 / ORCH-2 / VER-5 | High | **`search.worktree` is dead config.** Parsed and documented, never read; agents + verification run in the live `cwd` with the full inherited env. A user setting `worktree: true` gets no isolation. | `config.py:29,83`, `orchestrator.py:80,103` |
| DIFF-1 | Med | **`git diff --binary` leaks the whole tree** (unrelated WIP, secrets) into persisted patches, and accumulates. Simultaneously over-broad (unrelated tracked files) and incomplete (misses untracked agent output). | `orchestrator.py:147-160`, `state.py:75-76` |

> Isolation is the keystone: fixing it repairs patch artifacts, the size penalty, verification independence, *and* unblocks behavioral voting and parallelism. **Note the coupling the evaluators caught:** counting `output_bucket` votes is *invalid until isolation exists*, because today buckets reflect cumulative tree state.

### Theme C — Selection is largely hollow
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| SEL-1 | High | **Behavioral voting is vaporware.** `behavioral_vote_count` is hardcoded `1`; `output_bucket` is never clustered or counted; `selector: behavioral_vote` is shipped but `select_winner` has one fixed formula and never reads the knob. The marquee RSI selection feature is naming only. | `orchestrator.py:198`, `selector.py:25,34`, `.rsi.yaml:15` |
| SEL-2 | High | **`test_pass_rate` is a per-*command* fraction, not per-test**, and with the shipped single-command config it's binary and perfectly correlated with `hard_pass` — the "most informative" soft term carries zero extra information. | `orchestrator.py:189-196`, `.rsi.yaml:5-6` |
| SEL-3 | Med | `risk_score` = exact inverse of `hard_pass` → dead weight. | `orchestrator.py:200` |
| SEL-4 | Med | `static_quality_score` = flat `0.5` "patch exists" flag, not a quality measure. | `orchestrator.py:199` |
| SEL-5 | Med | **Ties resolved by lexicographic `candidate_id`** (largest wins) → biased toward whichever expert id sorts last, not the smaller/safer patch. | `selector.py:38` |
| SEL-7 / floor | Med | No minimum-evidence floor: when *all* candidates fail, a "winner" with zero passing evidence is still emitted with no `any_candidate_passed` flag. | `selector.py:34-38`, `orchestrator.py:88-91` |

> Net effect today: selection ≈ `hard_pass + binary test_pass_rate − penalties`, ties broken by id. (Subtlety the verifier flagged: the two penalties are clamped to `[0,10]`, not `[0,1]`, so a large/slow patch can subtract up to ~150 pts — penalties, not positive signals, dominate tie-breaking among passers.)

### Theme D — Verification doesn't realize the full gate
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| VER-1 | Med* | `verify.memory_mb` parsed/defaulted but **never enforced** (no rlimit/Job Object). *(\*high vs the article's memory gate; medium as documented scope.)* | `config.py:20,76`, `verifier.py:59-90` |
| VER-2 | Med* | **No runtime hard gate** — runtime is only a saturating soft penalty; a correct-but-slow candidate keeps `hard_pass`. | `verifier.py:51`, `orchestrator.py:192` |
| VER-4 | Med | `output_bucket` hashes **raw** stdout/stderr → over-sensitive to timestamps/paths/ordering, blind to file side-effects. Not a sound equivalence key (and unused). | `verifier.py:93-104` |
| VER-3 | Med | `timeout = command.timeout_sec or self.timeout_sec` → an explicit `timeout_sec: 0` is silently replaced by the 180s default. | `verifier.py:60`, `config.py:91-94` |
| VER-6 / SHELL-1 | High | `subprocess.run(shell=True)` over `.rsi.yaml`-supplied command strings with the **full inherited env**, **auto-triggered by the PostToolUse hook** after any edit → RCE/secret-exfil surface on cloned/untrusted repos. | `verifier.py:63-72`, `hooks.py:74-78` |
| VER-7 | Low | No determinism controls (no `PYTHONHASHSEED`/`LC_ALL`/`TZ`, no flake detection); wall-clock timing feeds scoring. | `verifier.py:48-50,79` |

### Theme E — Robustness & failure signaling
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| CLI-1 / RUN-1 | High | **`rsi run` always exits 0**, even when the winner failed verification. CI/automation/an RSI driver can't detect failure. (`winner.hard_pass` is already serialized — the fix is one line.) | `cli.py:137-149` |
| ROB / ORCH-3 | High | **Uncaught agent failure aborts the whole run.** A missing CLI (`FileNotFoundError`) or agent timeout (`TimeoutExpired`, hardcoded 900s, config-ignored) propagates uncaught; in-memory scores + `selection.json` are lost. *(Partially-confirmed: prior candidates' on-disk artifacts survive, but the run produces no selectable winner.)* | `adapters.py:15,40-56`, `orchestrator.py:93-104` |
| CFG-1 | High | `_load_structured_data` catches **bare `Exception`** and silently falls back to the lenient minimal parser → malformed config is silently truncated rather than rejected (verified: a key can vanish). | `config.py:101-109` |
| CFG-2 | High | **Two divergent YAML parsers** disagree on types (`0123`→83 vs 123; `1e3`→str vs 1000.0), so the same config behaves differently with/without PyYAML. | `config.py:97-210` |
| CFG-3 | Med | `config_from_dict` raises bare `KeyError`/`ValueError` (raw tracebacks naming neither file nor key). | `config.py:58-86` |
| CLI-2 / JSON-1 | High | **No top-level error handling and no uniform `--json` envelope**; four ad-hoc shapes, and errors print plain text to stderr even under `--json`. | `cli.py:52-68,119-164` |
| empty-set / `--rounds 0` | Med | `search.rounds: 0` (or empty experts) → empty scores → uncaught `ValueError` in `select_winner`; `--rounds 0` is silently coerced to the default by `or`. | `selector.py:34-36`, `cli.py:139` |
| ADP-1 | Med | `{prompt}` only substitutes on an exact-match token; `--msg={prompt}` leaks the literal **and** duplicates the prompt as a trailing arg. | `adapters.py:32-38` |
| CLI-3 | Med | `cmd_init` writes files one-by-one → can leave a half-initialized workspace. | `cli.py:106-112` |
| ADP-2 / STATE-1 / ORCH-2(rob) | Low | `_split_command` posix flag diverges across OSes; `_new_id` has no real collision resistance under `mkdir(exist_ok=False)`; a missing expert id becomes the literal string `"None"`. | `adapters.py:82-83`, `state.py:107-110`, `orchestrator.py:34-43` |

### Theme F — Security hardening
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| SHELL-1 | High | (see Theme D) config-as-code via `shell=True` + inherited env + auto-run hook. | `verifier.py:63-72`, `hooks.py:74-78` |
| ISO-1 | High | (see Theme B) untrusted agent patches run in the live checkout, no sandbox. | `orchestrator.py:64-104` |
| DENY-1 | Med | `DESTRUCTIVE_PATTERNS` is a 4-regex denylist, trivially bypassable (`rm -rf ./`, `rm -r -f /`, `find . -delete`, `> file`), only inspects `tool_input.command` (misses Edit/Write), and **returns exit 0** — enforcement depends entirely on the host honoring the printed JSON. | `hooks.py:15-20,53-67,117-121` |
| MCP-traversal | Med | **(missed by evaluators)** Unauthenticated `rsi.select_winner` passes `task_id` straight into `task_dir(task_id)` with no sanitization → `../`-style arbitrary `selection.json` read. | `mcp_server.py:82-85`, `state.py:53` |
| MCP-1 | Med | One malformed stdin line crashes the whole MCP loop (`json.loads` outside the try/except). | `mcp_server.py:42-48` |
| MCP-2 | Med | MCP server is unauthenticated and runs `verify`/`create_task` (a second trigger for `shell=True` config commands). | `mcp_server.py:51-86` |

### Theme G — Integration & DX
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| INIT-1 | High | **`rsi init` writes a Rust/npm config** (`src/**/*.rs`→cargo, `packages/web/**`→npm, name `tests`) that contradicts both this Python repo's committed `.rsi.yaml` and the README's documented sample. The first onboarding command misconfigures. | `cli.py:16-33` vs `.rsi.yaml`, `README.md:131-151` |
| HOOK-1 | High | `_emit_context` hardcodes `hookEventName="SessionStart"` for **every** event, so the PostToolUse failure-surfacing and the Stop gate (the two feedback-loop-critical hooks) can be silently dropped by the client. | `hooks.py:98-108` |
| JSON-1 | High | (see Theme E) non-uniform `--json`, errors never JSON. | `cli.py` |
| MCP-1(dx) | Med | Tools declared dotted (`rsi.create_task`) but surface underscored (`mcp__rsi-harness__rsi_create_task`); `.mcp.json` dual-keyed (`mcp_servers`/`mcpServers`) can desync; `mcp_server` has a dead `cmd_select` import. | `mcp_server.py:8,17`, `.mcp.json:2,8` |
| RUN-1 | Med | `rsi run` can't signal success (exit 0) and shows no progress during minutes-long searches. | `cli.py:137-149` |
| HOOK-2 / MANIFEST-1 | Low | `hooks.json` hardcodes bare `python`; Codex/Claude manifests diverge on the `skills` key (intentional per CLAUDE.md, but undocumented in README); shared matcher uses Codex's `apply_patch` and misses Claude's `MultiEdit`. | `hooks.json:9`, manifests |

### Theme H — Testing & quality gates
| ID | Sev | Finding | Evidence |
|---|:--:|---|---|
| TEST-ORCH-1 | High | **`Orchestrator.run` is completely untested**, even via the ready-made `dry_run` path — the RSI loop's core has zero coverage. | `orchestrator.py:53-104` |
| TEST-CFG-1 | High | The minimal-YAML fallback (~100 lines, the riskiest code) is **never executed by its own test** because PyYAML is installed in the dev env. The test name is misleading. | `config.py:112-210`, `tests/test_rsi_harness.py:17` |
| TEST-HOOK-1 / gate | High | `hooks.run_hook` and the destructive-command gate are untested; the gate **returns 0 even when blocking**, so a regression dropping the JSON would silently disable the only safety control with no test signal. | `hooks.py:23-131` |
| CI-1 | High* | **No CI, no mypy, no lint** despite pervasive type hints and a `.mypy_cache` gitignore entry — and the harness doesn't dogfood its own `compile/test/typecheck/lint` thesis. | `pyproject.toml`, `.rsi.yaml` |
| TEST-MCP-1 / VER-1 / DI-1 | Med | MCP handler, verifier timeout path, and `verify --changed` untested; no `FakeAgentDriver` seam for the non-dry-run path. | — |
| compact_feedback / score_from_report | Med | These pure functions central to the loop have no unit tests. | `orchestrator.py:177-203` |

---

## 6. Dependency graph of the fixes

```
                         ┌────────────────────────────────────────┐
   (keystone)  ORCH-1/ISO-1  per-candidate ISOLATION (worktree)    │
                         └───────┬───────────────┬────────────────┘
              unblocks ──────────┤               ├──────── unblocks
                                 ▼               ▼
            clean per-candidate diffs      independent verification
                  (DIFF-1)                       │
                                 ▼               ▼
                       real behavioral voting (SEL-1, VER-4)
                                 │               │
                                 ▼               ▼
                       safe PARALLELISM (ORCH-6)   correct patch_size_penalty
```

Most "make selection real" and "go parallel" work is **blocked on isolation**. Do isolation first.

---

## 7. Enhancement plan (phased, dependency-aware)

### Phase 0 — Honesty + cheap correctness  *(hours; mostly S-effort, no new architecture)*
1. **`rsi run` exit code:** `return 0 if selection['winner'].get('hard_pass') else 1`. *(CLI-1/RUN-1)*
2. **Catch agent failures:** wrap `AgentDriver.run` in `try/except (FileNotFoundError, TimeoutExpired)` → return a failed `DriverRun`; guard the per-expert body and the empty-`scores` `ValueError`. *(ROB, ORCH-3, empty-set)*
3. **Fail config loudly:** catch only `ModuleNotFoundError` for the no-PyYAML path; raise a `ConfigError` (file+key) on real YAML/coercion errors; replace `or` fallbacks with explicit `is None` checks. *(CFG-1, CFG-3, VER-3, `--rounds 0`)*
4. **Top-level CLI error handler** → emit `{"ok": false, "error": {...}}` to stdout under `--json`. *(CLI-2/JSON-1 start)*
5. **Fix `rsi init`** to write the Python config the README documents (or add `--lang` templates) and reconcile the README. *(INIT-1)*
6. **Fix `HOOK-1`** — map each event to its real `hookEventName`/output shape.
7. **Stop advertising dead knobs:** ship `selector: score` (the only implemented mode) and either implement `worktree`/`selector` or warn loudly when set; soften the package overclaim or add an inline scope note. *(RSI-2/3, SEL-6)*
8. **Hygiene:** remove dead imports (`cmd_select`, `HarnessConfig`); MCP `task_id` validation against `^T-\d+-[0-9a-f]{8}$`; wrap MCP `json.loads` per-line. *(MNT-3, MCP-traversal, MCP-1)*

### Phase 1 — Make the outer loop trustworthy  *(the keystone)*
9. **Per-candidate isolation** *(ORCH-1/ISO-1/ORCH-2/VER-5/DIFF-1)* — honor `search.worktree`: `git worktree add` (or clone) per candidate rooted at the task baseline, run the agent + verification there, capture the diff scoped to that worktree, tear down. Fallback: `git stash`/`reset --hard` between candidates. Refuse non-dry-run search on a dirty tree without `--force`.
10. **Harden the verify env** — pass a scrubbed/allowlisted env; default to `shlex` argv with explicit per-command `shell: true` opt-in; gate the PostToolUse auto-verify behind an opt-in marker. *(SHELL-1, VER-6)*

### Phase 2 — Make verification + selection *real*
11. **Behavioral voting** *(SEL-1/6, RSI-4, VER-4)* — cluster a round's candidates by `output_bucket` (now meaningful post-isolation), set `behavioral_vote_count` to a **normalized** cluster share, and branch `select_winner` on `config.search.selector`. First make the bucket robust (strip ANSI/timestamps/paths or hash test-result vectors).
12. **Real signals** — parse per-test pass counts for `test_pass_rate`; derive `static_quality_score` from lint/type counts; derive `risk_score` from diff churn/deletions; replace the id tie-break with merit keys; add an `any_candidate_passed`/low-confidence flag. *(SEL-2/3/4/5/7)*
13. **Performance gates** — optional `max_runtime_sec` excluded from `hard_pass`; enforce `memory_mb` (POSIX `RLIMIT_AS` / Windows Job Object) + record peak RSS. *(VER-1/2)*

### Phase 3 — Search quality + DX
14. **Per-lineage feedback** — key feedback per (expert, round) so a candidate repairs its *own* prior failure, not a sibling's. *(ORCH-4)*
15. **Real `prompt_variant` templates** via a small strategy registry. *(ORCH-5)*
16. **Parallelism** — run independent experts within a round concurrently (now safe), with a bounded pool, a total run budget, and a configurable agent timeout. *(ORCH-6, AGT-1)*
17. **DX** — uniform `--json` envelope + schema; `rsi run` progress to stderr; underscore MCP tool names; collapse `.mcp.json` to one source; cover `MultiEdit`; document the denylist as advisory + prefer a confirm/allowlist model. *(JSON-1, RUN-1, MCP-1, DENY-1)*
18. **Search-side `changed_file_rules`** (including untracked files) so per-candidate verification is scoped. *(VER/ORCH missed)*

### Phase 4 — Toward the *recursion* (the article's actual thesis)
19. **`rsi learn` / `CorpusStats`** — read `.rsi/tasks/**` to compute per-expert win rates, recurring failure signatures, and bucket frequencies. **Read-only first** — the smallest honest step that turns the write-only corpus into a feedback source. *(RSI-1)*
20. **Feed the corpus back** into expert ordering/weighting, selector priors, and prompt construction ("past failures on similar specs").
21. **`PromptStrategy`/`VerifierStrategy` seams** — pluggable, name-resolved, so a meta-layer can substitute optimized strategies. *(RSI-5)*
22. **Meta-loop (research frontier)** — propose/optimize prompts, expert panels, and verifier commands from corpus outcomes. This is the genuine "recursive self-improvement," and the part the article keeps proprietary — frame it as a stretch goal, not a checkbox.

### Cross-cutting — Testing & CI  *(do alongside every phase)*
- **CI workflow:** `unittest` + `mypy` + `ruff` on 3.10–3.12; add `[tool.mypy]`/`[tool.ruff]`; extend the repo's own `.rsi.yaml` to include typecheck/lint (dogfood the thesis). *(CI-1)*
- **`Orchestrator` dry-run E2E test** + a `FakeAgentDriver` injection seam (`driver_factory`). *(TEST-ORCH-1, DI-1)*
- **Hooks table tests** incl. the destructive-gate *contract* (assert the printed deny JSON, since exit is always 0); **MCP handler tests**; **direct minimal-YAML parser tests** + force the no-PyYAML branch; **verifier timeout/empty-list/bucket tests**; **`compact_feedback`/`score_from_report` unit tests**; pin the selector weights (with `behavioral_vote_count=0` to actually get 1000).

---

## 8. Quick-win checklist (S-effort, high-value — start here)

- [ ] `rsi run` returns non-zero when the winner didn't `hard_pass`.
- [ ] Catch `FileNotFoundError`/`TimeoutExpired` in the agent driver; never let one expert kill the batch.
- [ ] `_load_structured_data`: catch only `ModuleNotFoundError`; raise a clear `ConfigError` otherwise.
- [ ] Fix `HOOK-1` event labels (restores the feedback loop).
- [ ] Make `rsi init` emit the Python config the README shows; reconcile the docs.
- [ ] Ship `selector: score`; warn (don't silently ignore) on `worktree: true` / `behavioral_vote` until implemented.
- [ ] Validate MCP `task_id`; wrap per-line `json.loads`.
- [ ] Top-level CLI JSON error envelope.
- [ ] Add CI (`unittest` + `ruff` + `mypy`) and a `dry_run` orchestrator test.

---

*Severities reflect the adversarial-verification pass (corrected where the skeptic disagreed). Findings marked "missed" were surfaced by the completeness critics, not the primary evaluators.*
