# 阶段 4：golden 工具实测（对正式 baseline 批次跑全局理解 + 逐条标注）
# 用法（PowerShell，在项目根目录）：
#   powershell -File scripts\run_golden.ps1
# 输入：runs\<BATCH>\traces（skill 驱动版 baseline 345 条）+ data\skills_flat（15 个 skill 文档）
# 输出：sut\runs\golden\<ts>\golden_output.jsonl + sut\runs\GU\<ts>\global_understanding.txt
# LLM：读 sut\.env（glm-5.1），真实调用
# 日志：scripts\logs\golden_<ts>.log
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path "scripts\logs" | Out-Null
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$log = "scripts\logs\golden_$ts.log"
Write-Host "日志: $log"

# 正式 baseline 批次（skill 驱动版）。如需换批次：$env:BATCH="xxx"; powershell -File scripts\run_golden.ps1
$batch = if ($env:BATCH) { $env:BATCH } else { "20260903_105240_strong_skilldriven_baseline_full" }
$traceDir = "runs\$batch\traces"
if (-not (Test-Path $traceDir)) {
  Write-Error "轨迹目录不存在: $traceDir（可用 `$env:BATCH 覆盖）"
  exit 1
}
$n = (Get-ChildItem $traceDir -File).Count
Write-Host "输入轨迹: $traceDir（$n 条）"

Set-Location sut
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$env:LLM_DEBUG = "1"
& .venv\Scripts\python.exe run.py golden `
  --trace-dir "..\$traceDir" `
  --skill-cache-dir ..\data\skills_flat `
  --policy-file ..\agent\tau_bench\tau_bench\envs\retail\wiki.md `
  --regenerate-global `
  --batch-size 10 `
  2>&1 | Tee-Object -FilePath "..\$log" -Append
Write-Host "完成。日志: $log；产出见 sut\runs\golden\ 与 sut\runs\GU\ 最新时间戳目录"
