---
name: rsi-coding
description: Use for complex coding tasks that benefit from recursive repair, executable validation, multiple candidates, tests, benchmarks, or selector voting.
---

# RSI Coding Workflow

Use the RSI harness when correctness, regression risk, runtime, memory, or implementation quality should be judged by executable evidence instead of model confidence.

1. Convert the user task into a measurable task spec:
   - expected behavior
   - affected files
   - validation commands
   - acceptance criteria
   - residual risk that cannot be verified automatically

2. Prefer executable evidence:
   - compile
   - unit tests
   - integration tests
   - typecheck
   - lint
   - fuzz/property tests when applicable
   - benchmark for performance-sensitive code

3. After each code change, call:

   `rsi verify --changed --json`

4. Treat feedback as the next-round prompt:
   - minimal failing command
   - stack trace excerpt
   - failing assertion
   - relevant diff hunk
   - runtime/memory signal

5. For difficult tasks, use batch search:

   `rsi run --task issue.md --experts experts.yaml --rounds 3`

6. Finish only with:
   - winning diff or selected candidate
   - verification report
   - residual risks
   - exact commands run

