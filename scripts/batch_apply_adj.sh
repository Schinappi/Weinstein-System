#!/bin/bash
# Batch apply adj_factor in groups of 500 to avoid OOM
cd /home/admin/Weinstein-System
source .venv/bin/activate

TOTAL=5701
BATCH=500
SLEEP=2

for ((start=1; start<=TOTAL; start+=BATCH)); do
    end=$((start + BATCH - 1))
    if [ "$end" -gt "$TOTAL" ]; then
        end=$TOTAL
    fi
    echo ""
    echo "========== Batch $start - $end / $TOTAL =========="
    PYTHONUNBUFFERED=1 python scripts/apply_adj_factor.py --start-idx "$start" --end-idx "$end"
    RC=$?
    echo "Batch exit code: $RC"
    if [ "$RC" -ne 0 ]; then
        echo "Batch failed, sleeping 5s before retry..."
        sleep 5
        PYTHONUNBUFFERED=1 python scripts/apply_adj_factor.py --start-idx "$start" --end-idx "$end"
        RC=$?
        echo "Retry exit code: $RC"
    fi
    sleep "$SLEEP"
done

echo ""
echo "=== All batches done ==="
