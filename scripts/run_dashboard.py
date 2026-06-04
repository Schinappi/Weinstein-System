#!/usr/bin/env python3
"""Run the Weinstein dashboard server."""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from winstan.dashboard.server import run_server

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    config_path = Path(__file__).resolve().parent.parent / "config" / "strategy.yaml"
    run_server(config_path, host, port)
