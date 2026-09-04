# skill 驱动版正式 baseline 全量跑批（115 任务 × 3 trials，qwen-turbo + GLM5.1 用户模拟器）
# 用法（PowerShell，在项目根目录）：
#   powershell -File scripts\run_skilldriven_baseline_full.ps1
# 输出：runs\<ts>_strong_skilldriven_baseline_full\{raw,traces,tau_reward.json,...}
# 日志：scripts\logs\skilldriven_baseline_full_<ts>.log
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path "scripts\logs" | Out-Null
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$log = "scripts\logs\skilldriven_baseline_full_$ts.log"
Write-Host "日志: $log"
$env:PYTHONIOENCODING = "utf-8"
# 网络瞬断（DNS/连接失败）会导致整批中断，最多自动重试 3 次
foreach ($attempt in 1..3) {
  Write-Host "===== 第 $attempt 次尝试 ====="
  & agent\.venv\Scripts\python.exe agent\run_batch.py `
    --tier strong --suffix skilldriven_baseline_full `
    --prompt-mode skill `
    --model openai/qwen-turbo --user-model openai/glm-5.1 `
    --start 0 --end 115 --num-trials 3 --temperature 0.6 --max-concurrency 8 `
    2>&1 | Tee-Object -FilePath $log -Append
  if ($LASTEXITCODE -eq 0) {
    Write-Host "完成。日志: $log"
    exit 0
  }
  Write-Host "第 $attempt 次失败，30s 后重试..."
  Start-Sleep -Seconds 30
}
Write-Host "3 次尝试均失败，请检查网络后手动重跑。日志: $log"
exit 1
