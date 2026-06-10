#!/usr/bin/env bash
# ==============================================================
# 完整 Phase1 重跑脚本
# 用途: 重新运行温斯坦筛选器，生成最新 screening_results
#       并在完成后清除 Dashboard 缓存
# 用法: bash scripts/rerun_phase1.sh [--push]
#   --push  同时推送代码到 GitHub
# ==============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/rerun_phase1_$(date +%Y%m%d_%H%M).log"
VENV="${PROJECT_ROOT}/.venv"

echo "[$(date '+%H:%M:%S')] ===== Phase1 重跑开始 =====" | tee -a "$LOG_FILE"
echo "[$(date '+%H:%M:%S')] 项目目录: ${PROJECT_ROOT}" | tee -a "$LOG_FILE"

# 1. 激活 venv
source "${VENV}/bin/activate"

# 2. 刷新 DuckDB 视图（确保 parquet 一致）
echo "[$(date '+%H:%M:%S')] 刷新 DuckDB parquet 视图..." | tee -a "$LOG_FILE"
python -c "
from pathlib import Path
from winstan.storage.duckdb_store import DuckDBStore
store = DuckDBStore(Path('data/duckdb/market_data.duckdb'))
store.refresh_parquet_view('daily_bars', str(Path('data/parquet/daily_bars/*.parquet')))
store.refresh_parquet_view('index_bars', str(Path('data/parquet/index_bars/*.parquet')))
print('[ok] DuckDB views refreshed')
" 2>&1 | tee -a "$LOG_FILE"

# 3. 运行 Phase1 筛选
echo "[$(date '+%H:%M:%S')] 运行 Phase1 筛选..." | tee -a "$LOG_FILE"
START_TS=$(date +%s)
python -c "
import time, json
from pathlib import Path
import sys
sys.path.insert(0, 'src')
from winstan.config import load_config
from winstan.pipeline.screener import WeinsteinScreener

t0 = time.time()
config = load_config(Path('config/strategy.yaml'))
result = WeinsteinScreener(config).run()
elapsed = time.time() - t0
results = result['results']
stage2 = result['stage2_top_n']
s2_candidate = int(results['stage2_candidate'].sum()) if 'stage2_candidate' in results.columns else 0
print(json.dumps({
    'elapsed_seconds': round(elapsed, 1),
    'results_count': int(len(results)),
    'stage2_candidate': s2_candidate,
    'stage2_top_n': int(len(stage2)),
    'trade_date': str(results['trade_date'].max()) if not results.empty else None,
}, ensure_ascii=False))
" 2>&1 | tee -a "$LOG_FILE"
END_TS=$(date +%s)
echo "[$(date '+%H:%M:%S')] Phase1 耗时: $((END_TS - START_TS)) 秒" | tee -a "$LOG_FILE"

# 4. 清除 Dashboard 内存缓存
echo "[$(date '+%H:%M:%S')] 清除 Dashboard 缓存..." | tee -a "$LOG_FILE"
curl -s -X POST http://localhost:8765/api/dashboard/refresh | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'[ok] cache refreshed: {d}')" 2>&1 | tee -a "$LOG_FILE"

# 5. 可选推送到 GitHub
if [[ "${1:-}" == "--push" ]]; then
    echo "[$(date '+%H:%M:%S')] 推送代码到 GitHub..." | tee -a "$LOG_FILE"
    git add -A
    git commit -m "chore: Phase1 rerun $(date +%Y-%m-%d)" 2>/dev/null || echo "  (无新变更跳过)"
    git push 2>&1 | tee -a "$LOG_FILE"
    echo "[$(date '+%H:%M:%S')] 推送完成" | tee -a "$LOG_FILE"
fi

echo "[$(date '+%H:%M:%S')] ===== Phase1 重跑完成 =====" | tee -a "$LOG_FILE"
echo "日志: ${LOG_FILE}"
