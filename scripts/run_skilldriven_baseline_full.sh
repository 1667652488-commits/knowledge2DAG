# skill 驱动版正式 baseline 全量跑批（115 任务 × 3 trials，qwen-turbo + GLM5.1 用户模拟器）
# 用法（Git Bash，在项目根目录）：
#   bash scripts/run_skilldriven_baseline_full.sh
# 输出：runs/<ts>_strong_skilldriven_baseline_full/{raw,traces,tau_reward.json,...}
# 日志：scripts/logs/skilldriven_baseline_full_<ts>.log（终端输出同时落盘，方便回看）
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p scripts/logs
TS=$(date +%Y%m%d_%H%M%S)
LOG="scripts/logs/skilldriven_baseline_full_$TS.log"
echo "日志: $LOG"
# 网络瞬断（DNS/连接失败）会导致整批中断，最多自动重试 3 次
for attempt in 1 2 3; do
  echo "===== 第 $attempt 次尝试 ====="
  if PYTHONIOENCODING=utf-8 agent/.venv/Scripts/python agent/run_batch.py \
    --tier strong --suffix skilldriven_baseline_full \
    --prompt-mode skill \
    --model openai/qwen-turbo --user-model openai/glm-5.1 \
    --start 0 --end 115 --num-trials 3 --temperature 0.6 --max-concurrency 8 \
    2>&1 | tee -a "$LOG"; then
    echo "完成。日志: $LOG"
    exit 0
  fi
  echo "第 $attempt 次失败，30s 后重试..."
  sleep 30
done
echo "3 次尝试均失败，请检查网络后手动重跑。日志: $LOG"
exit 1
