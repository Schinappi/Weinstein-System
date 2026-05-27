from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.config import load_config
from winstan.outputs.reporting import export_results
from winstan.pipeline.screener import WeinsteinScreener


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 1 Weinstein screener with AI-style analysis reports.")
    parser.add_argument(
        "--config",
        default="config/strategy.yaml",
        help="Path to strategy config file.",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    result = WeinsteinScreener(config).run()
    export_results(
        config,
        result["results"],
        result["candidates"],
        result["top_n"],
        result["stage2_top_n"],
        result["summary"],
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
