"""Recursive self-improvement coding harness.

Implements the outer verify-and-select loop of an RSI system: external agents
generate candidate patches, the harness runs hard verification and selects the
best candidate by executable evidence. It does not (yet) learn across tasks or
self-optimize its own prompts/selector/verifier; the persisted ``.rsi/tasks``
corpus is the intended extension point for that.
"""

__version__ = "0.1.0"

