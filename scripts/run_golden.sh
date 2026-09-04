# 阶段 4：golden 工具实测（对正式 baseline 批次跑全局理解 + 逐条标注）
# 用法（Git Bash，在项目根目录）：
#   bash scripts/run_golden.sh
# 输入：runs/<BATCH>/traces（skill 驱动版 baseline 345 条）+ data/skills_flat（15 个 skill 文档）
# 输出：sut/runs/golden/<ts>/golden_output.jsonl + sut/runs/GU/<ts>/global_understanding.txt
# LLM：读 sut/.env（LLM_MODE=openai, glm-5.1），真实调用，预计 345 条 × 若干次调用
# 日志：scripts/logs/golden_<ts>.log
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p scripts/logs
TS=$(date +%Y%m%d_%H%M%S)
LOG="scripts/logs/golden_$TS.log"
echo "日志: $LOG"

# 正式 baseline 批次（skill 驱动版）。如需换批次，运行时覆盖：BATCH=xxx bash scripts/run_golden.sh
BATCH="${BATCH:-20260903_105240_strong_skilldriven_baseline_full}"
TRACE_DIR="runs/$BATCH/traces"
if [ ! -d "$TRACE_DIR" ]; then
  echo "轨迹目录不存在: $TRACE_DIR（可用 BATCH=<批次名> 覆盖）" >&2
  exit 1
fi
echo "输入轨迹: $TRACE_DIR（$(ls "$TRACE_DIR" | wc -l) 条）"

cd sut
PYTHONIOENCODING=utf-8 .venv/Scripts/python run.py golden \
  --trace-dir "../$TRACE_DIR" \
  --skill-cache-dir ../data/skills_flat \
  --regenerate-global \
  --batch-size 10 \
  2>&1 | tee -a "../$LOG"
echo "完成。日志: $LOG；产出见 sut/runs/golden/ 与 sut/runs/GU/ 最新时间戳目录"
