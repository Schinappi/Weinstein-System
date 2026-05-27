from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.dashboard.server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Weinstein dashboard server.")
    parser.add_argument("--config", default="config/strategy.yaml", help="Path to strategy config file.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    args = parser.parse_args()

    run_server(args.config, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
