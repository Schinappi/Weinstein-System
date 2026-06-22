"""Quick incremental daily bar update — bulk fetch all A-share in 1 API call."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.config import load_config
from winstan.data.daily_updater import bulk_update_recent
from winstan.storage.parquet_store import ParquetStore
from winstan.adapters.tushare_client import build_tushare_pro


def main() -> None:
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    _, pro = build_tushare_pro(config.data.tushare_token)

    result = bulk_update_recent(pro, store, days_back=5)
    print(f"\n=== Done ===")
    print(f"API call: {result['api_seconds']:.1f}s (just 1 call!)")
    print(f"Merge: {result['merge_seconds']:.0f}s")
    print(f"Updated: {result['updated']}, Unchanged: {result['unchanged']}, "
          f"Errors: {result['errors']}")
    print(f"Rows added: {result['rows_added']}")


if __name__ == "__main__":
    main()
