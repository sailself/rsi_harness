#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    plugin_root = (
        os.environ.get("PLUGIN_ROOT")
        or os.environ.get("CLAUDE_PLUGIN_ROOT")
        or str(Path(__file__).resolve().parents[1])
    )
    sys.path.insert(0, plugin_root)
    from rsi_harness.hooks import run_hook

    if len(sys.argv) < 2:
        print("usage: rsi_hook.py <session-start|prompt-submit|pre-tool|post-tool|stop>", file=sys.stderr)
        return 2
    return run_hook(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())

