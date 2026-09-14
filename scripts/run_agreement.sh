# 对拍①：golden 标注 vs 硬判分一致率（表格 + 落盘 runs/<ts>_agreement_*/）
# 用法（Git Bash，项目根目录）：
#   bash scripts/run_agreement.sh                      # 自动找最新 golden 产出 + 默认 baseline 批次
#   GOLDEN=<路径> BATCH=<批次名> bash scripts/run_agreement.sh   # 手动指定
set -euo pipefail
cd "$(dirname "$0")/.."

BATCH="${BATCH:-20260903_105240_strong_skilldriven_baseline_full}"

# 自动找最新 golden 产出：优先 runs/*_golden/，其次 sut/runs/golden/<ts>/
if [ -z "${GOLDEN:-}" ]; then
  GOLDEN=$(ls -td runs/*_golden/golden_output.jsonl 2>/dev/null | head -1 || true)
  if [ -z "$GOLDEN" ]; then
    GOLDEN=$(ls -td sut/runs/golden/*/golden_output.jsonl 2>/dev/null | head -1 || true)
  fi
fi
if [ -z "$GOLDEN" ]; then
  echo "找不到 golden 产出（runs/*_golden/ 或 sut/runs/golden/*/），请用 GOLDEN=<路径> 指定" >&2
  exit 1
fi
echo "golden: $GOLDEN"
echo "批次:   runs/$BATCH"

python eval/reward_agreement.py "$GOLDEN" "runs/$BATCH"
