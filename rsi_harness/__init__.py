"""Recursive self-improvement coding harness.

Implements the outer verify-and-select loop of an RSI system: external agents
generate candidate patches, the harness runs hard verification and selects the
best candidate by executable evidence. With ``search.use_corpus`` it also closes
a practical cross-task loop, reading the persisted ``.rsi/tasks`` corpus (see
``rsi learn``) to order experts by past win-rate and seed prompts with recurring
failures. It does not yet auto-optimize its own verifier strategies — that
remains the frontier.
"""

__version__ = "0.1.0"

